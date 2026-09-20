"""
test_weaknesses.py -- Audit suite for three suspected weaknesses in SafeAgentDB
(plus one found while reading the code).

Everything here runs on plain pytest in a couple of seconds: file-based SQLite
in ``tmp_path`` stands in for "production", and dialect-specific behaviour is
exercised by building ``MetaData`` with postgresql types directly in Python and
calling the internal functions on it. No server, no network, no Docker.

Each claim is covered by two kinds of test:

* ``test_observed_*`` -- asserts the behaviour the library has **today**.
  These PASS. They are the proof that the weakness is (or is not) real.
* ``test_expected_*`` -- asserts the behaviour a safe library **should** have.
  These are marked ``xfail(strict=True)``: they fail today, and the moment the
  underlying bug is fixed they XPASS, which strict mode turns into an error --
  a reminder to drop the marker and keep the test as a regression guard.
  Run ``pytest --runxfail`` to see them as ordinary hard failures.

No library code is modified by this file.
"""

from __future__ import annotations

from typing import Literal

import pytest
from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from safeagentdb import SafeModel, ShadowDB, SyncError
from safeagentdb.engine import (
    _detect_dialect,
    clone_schema_to_sandbox,
    create_sandbox_engine,
    reflect_tables,
)
from safeagentdb.models import _model_registry


# ============================================================
# Fixtures & helpers
# ============================================================


@pytest.fixture(autouse=True)
def _isolated_registry():
    """The SafeModel registry is process-global; keep every test independent."""
    saved = dict(_model_registry)
    _model_registry.clear()
    yield
    _model_registry.clear()
    _model_registry.update(saved)


def _prod_engine(tmp_path, name: str, ddl: list[str]):
    """A file-based SQLite 'production' database.

    File-based rather than ``:memory:`` so that a second engine really is a
    separate connection, the way another process would be.
    """
    url = f"sqlite:///{tmp_path / name}"
    engine = create_engine(url)
    with engine.begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))
    return engine, url


def _sandbox_ddl(engine) -> str:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
        ).fetchall()
    return "\n".join(r[0] for r in rows)


def _constraint_kinds(table: Table) -> set[str]:
    return {type(c).__name__ for c in table.constraints}


def _build_metadata(*, postgres_typed: bool) -> MetaData:
    """The same logical schema twice: once with a postgres-specific column type,
    once with only generic types. Everything else is identical."""
    meta = MetaData()
    extra = [Column("prefs", JSONB)] if postgres_typed else []
    Table(
        "users",
        meta,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, nullable=False),
        Column("email", String(255), nullable=False, unique=True),
        Column("plan", String(32), nullable=False, server_default="free"),
        Column("age", Integer),
        *extra,
        CheckConstraint("age >= 0", name="ck_users_age_nonneg"),
    )
    Table(
        "tasks",
        meta,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer, nullable=False),
        Column("owner_id", Integer, ForeignKey("users.id"), nullable=False),
        Column("title", String(255), nullable=False),
    )
    return meta


def _try_sql(engine, sql: str) -> str:
    """Run a statement in the sandbox; return 'ACCEPTED' or the error class name."""
    with engine.connect() as conn:
        try:
            conn.execute(text(sql))
            conn.commit()
            return "ACCEPTED"
        except Exception as exc:  # noqa: BLE001 -- the class name is the result
            conn.rollback()
            return type(exc).__name__


TASKS_DDL = [
    "CREATE TABLE tasks ("
    " id INTEGER PRIMARY KEY,"
    " user_id INTEGER NOT NULL,"
    " title TEXT NOT NULL,"
    " status TEXT NOT NULL)",
    "INSERT INTO tasks VALUES (1, 42, 'Ship v2', 'todo')",
]

EVENTS_DDL = [
    # Audit-log shaped table: no PRIMARY KEY at all. Legal in Postgres and
    # SQLite alike, and common for append-only logs.
    "CREATE TABLE events ("
    " user_id INTEGER NOT NULL,"
    " kind TEXT NOT NULL,"
    " payload TEXT NOT NULL)",
    "INSERT INTO events VALUES (42,'login','a'),(42,'click','b'),"
    "(42,'logout','c'),(99,'login','other-tenant')",
]


def _register_task_validator():
    class TaskValidator(SafeModel):
        __table_name__ = "tasks"
        id: int
        user_id: int
        title: str
        status: Literal["todo", "in_progress", "done"]

    return TaskValidator


def _register_event_validator():
    class EventValidator(SafeModel):
        __table_name__ = "events"
        user_id: int
        kind: str
        payload: str

    return EventValidator


# ============================================================
# CLAIM 1 -- Schema fidelity of the sandbox clone
#
# Code path: engine._clone_table_for_sqlite() copies only
# (name, type, primary_key, nullable); engine.clone_schema_to_sandbox()
# selects it only when engine._detect_dialect() != "sqlite".
# ============================================================


class TestClaim1SchemaFidelity:
    # ---- Non-SQLite (postgres-typed) metadata: the claim holds ----

    def test_observed_pg_metadata_loses_unique_check_fk_and_default(self):
        """The clone keeps only the primary key. Everything else is dropped."""
        meta = _build_metadata(postgres_typed=True)
        assert _detect_dialect(meta) == "postgresql"

        # The source really does carry all four things.
        assert "UniqueConstraint" in _constraint_kinds(meta.tables["users"])
        assert "CheckConstraint" in _constraint_kinds(meta.tables["users"])
        assert "ForeignKeyConstraint" in _constraint_kinds(meta.tables["tasks"])
        assert meta.tables["users"].c.plan.server_default is not None

        sandbox = create_sandbox_engine()
        try:
            sb_meta = clone_schema_to_sandbox(meta, sandbox)

            assert _constraint_kinds(sb_meta.tables["users"]) == {"PrimaryKeyConstraint"}
            assert _constraint_kinds(sb_meta.tables["tasks"]) == {"PrimaryKeyConstraint"}
            assert sb_meta.tables["users"].c.plan.server_default is None
            assert sb_meta.tables["tasks"].c.owner_id.foreign_keys == set()

            ddl = _sandbox_ddl(sandbox)
            assert "UNIQUE" not in ddl
            assert "CHECK" not in ddl
            assert "FOREIGN KEY" not in ddl
            assert "DEFAULT" not in ddl
        finally:
            sandbox.dispose()

    def test_observed_pg_sandbox_accepts_data_production_would_reject(self):
        """An agent can do three things in the sandbox that Postgres would refuse."""
        meta = _build_metadata(postgres_typed=True)
        sandbox = create_sandbox_engine()
        try:
            clone_schema_to_sandbox(meta, sandbox)
            with sandbox.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO users (id,user_id,email,plan,age) "
                        "VALUES (1,42,'a@b.com','pro',30)"
                    )
                )
                conn.commit()

            # 1. duplicate value in a UNIQUE column
            assert (
                _try_sql(
                    sandbox,
                    "INSERT INTO users (id,user_id,email,plan,age) "
                    "VALUES (2,42,'a@b.com','pro',1)",
                )
                == "ACCEPTED"
            )

            # 2. value that violates the CHECK constraint
            assert (
                _try_sql(
                    sandbox,
                    "INSERT INTO users (id,user_id,email,plan,age) "
                    "VALUES (3,42,'c@d.com','pro',-5)",
                )
                == "ACCEPTED"
            )

            # 3. foreign key pointing at a row that does not exist
            assert (
                _try_sql(
                    sandbox,
                    "INSERT INTO tasks (id,user_id,owner_id,title) "
                    "VALUES (1,42,999,'orphan')",
                )
                == "ACCEPTED"
            )

            with sandbox.connect() as conn:
                emails = conn.execute(text("SELECT email FROM users")).fetchall()
                ages = conn.execute(text("SELECT age FROM users ORDER BY id")).fetchall()
            assert [e[0] for e in emails].count("a@b.com") == 2
            assert (-5,) in ages
        finally:
            sandbox.dispose()

    def test_observed_pg_sandbox_also_rejects_data_production_would_accept(self):
        """The dropped server default makes the sandbox stricter, not only looser:
        a NOT NULL column whose default was lost now rejects a legal INSERT."""
        meta = _build_metadata(postgres_typed=True)
        sandbox = create_sandbox_engine()
        try:
            clone_schema_to_sandbox(meta, sandbox)
            # In production `plan` would default to 'free'. Here the column is
            # NOT NULL with no default, so the same statement fails.
            assert (
                _try_sql(
                    sandbox,
                    "INSERT INTO users (id,user_id,email,age) VALUES (4,42,'e@f.com',1)",
                )
                == "IntegrityError"
            )
        finally:
            sandbox.dispose()

    # ---- SQLite production: the claim does NOT hold ----

    def test_observed_sqlite_production_does_preserve_constraints(self, tmp_path):
        """For a SQLite production DB, clone_schema_to_sandbox() takes the
        ``table.to_metadata()`` branch, which DOES carry the constraints over."""
        engine, _ = _prod_engine(
            tmp_path,
            "constraints.db",
            [
                # NB: the closing paren must sit on its own line -- see
                # test_observed_sqlite_check_reflection_can_crash_sandbox_creation.
                "CREATE TABLE users (\n"
                " id INTEGER PRIMARY KEY,\n"
                " user_id INTEGER NOT NULL,\n"
                " email TEXT NOT NULL UNIQUE,\n"
                " plan TEXT NOT NULL DEFAULT 'free',\n"
                " age INTEGER CHECK (age >= 0)\n"
                ")",
                "CREATE TABLE tasks (\n"
                " id INTEGER PRIMARY KEY,\n"
                " user_id INTEGER NOT NULL,\n"
                " owner_id INTEGER NOT NULL REFERENCES users(id),\n"
                " title TEXT NOT NULL\n"
                ")",
                "CREATE UNIQUE INDEX ux_users_email ON users(email)",
            ],
        )
        meta = reflect_tables(engine, ["users", "tasks"])
        assert _detect_dialect(meta) == "sqlite"

        sandbox = create_sandbox_engine()
        try:
            sb_meta = clone_schema_to_sandbox(meta, sandbox)

            assert "UniqueConstraint" in _constraint_kinds(sb_meta.tables["users"])
            assert "CheckConstraint" in _constraint_kinds(sb_meta.tables["users"])
            assert "ForeignKeyConstraint" in _constraint_kinds(sb_meta.tables["tasks"])
            assert sb_meta.tables["users"].c.plan.server_default is not None

            ddl = _sandbox_ddl(sandbox)
            assert "UNIQUE (email)" in ddl
            assert "CHECK (age >= 0)" in ddl
            assert "FOREIGN KEY(owner_id) REFERENCES users (id)" in ddl
            assert "DEFAULT 'free'" in ddl
            assert "CREATE UNIQUE INDEX ux_users_email" in ddl

            # ...and they are actually enforced.
            with sandbox.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO users (id,user_id,email,age) "
                        "VALUES (1,42,'a@b.com',30)"
                    )
                )
                conn.commit()
            assert (
                _try_sql(
                    sandbox,
                    "INSERT INTO users (id,user_id,email,age) VALUES (2,42,'a@b.com',1)",
                )
                == "IntegrityError"
            )
            assert (
                _try_sql(
                    sandbox,
                    "INSERT INTO users (id,user_id,email,age) VALUES (3,42,'c@d.com',-5)",
                )
                == "IntegrityError"
            )
            # The server default survives too.
            with sandbox.connect() as conn:
                conn.execute(
                    text("INSERT INTO users (id,user_id,email) VALUES (4,42,'e@f.com')")
                )
                conn.commit()
                plan = conn.execute(text("SELECT plan FROM users WHERE id=4")).scalar_one()
            assert plan == "free"
        finally:
            sandbox.dispose()

    def test_observed_sandbox_never_enforces_foreign_keys(self, tmp_path):
        """Even on the faithful SQLite path the FK is decorative: SQLite ships
        with ``PRAGMA foreign_keys=OFF`` and nothing in the library turns it on."""
        engine, _ = _prod_engine(
            tmp_path,
            "fk.db",
            [
                "CREATE TABLE users (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
                "CREATE TABLE tasks ("
                " id INTEGER PRIMARY KEY,"
                " user_id INTEGER NOT NULL,"
                " owner_id INTEGER NOT NULL REFERENCES users(id),"
                " title TEXT NOT NULL)",
            ],
        )
        meta = reflect_tables(engine, ["users", "tasks"])
        sandbox = create_sandbox_engine()
        try:
            clone_schema_to_sandbox(meta, sandbox)
            with sandbox.connect() as conn:
                assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 0
            assert (
                _try_sql(
                    sandbox,
                    "INSERT INTO tasks (id,user_id,owner_id,title) "
                    "VALUES (1,42,999,'orphan')",
                )
                == "ACCEPTED"
            )
        finally:
            sandbox.dispose()

    def test_observed_sqlite_check_reflection_can_crash_sandbox_creation(self, tmp_path):
        """Sub-finding: on the SQLite path the reflected CHECK expression is
        re-emitted verbatim. SQLAlchemy's SQLite CHECK reflection is regex-based
        and swallows the table's closing paren when it sits on the same line, so
        the sandbox DDL is unbalanced and ShadowDB.__enter__ raises.

        The only difference from the table above is where a newline sits.
        """
        from sqlalchemy.exc import OperationalError

        engine, _ = _prod_engine(
            tmp_path,
            "check_oneline.db",
            [
                "CREATE TABLE users ("
                " id INTEGER PRIMARY KEY,"
                " user_id INTEGER NOT NULL,"
                " age INTEGER CHECK (age >= 0))",
                "INSERT INTO users VALUES (1, 42, 30)",
            ],
        )

        meta = reflect_tables(engine, ["users"])
        (check,) = [
            c for c in meta.tables["users"].constraints if isinstance(c, CheckConstraint)
        ]
        # The reflected expression is not balanced.
        assert check.sqltext.text == "age >= 0)"

        with pytest.raises(OperationalError) as excinfo:
            with ShadowDB(engine, tables=["users"], tenant_id=42):
                pass
        assert "syntax error" in str(excinfo.value)

    # ---- The dividing line is an incidental detail of the schema ----

    @pytest.mark.parametrize(
        "postgres_typed,expected_constraints,duplicate_email_result",
        [
            (True, {"PrimaryKeyConstraint"}, "ACCEPTED"),
            (
                False,
                {"PrimaryKeyConstraint", "UniqueConstraint", "CheckConstraint"},
                "IntegrityError",
            ),
        ],
    )
    def test_observed_fidelity_flips_on_one_column_type(
        self, postgres_typed, expected_constraints, duplicate_email_result
    ):
        """Two identical Postgres schemas get different sandboxes purely because
        one of them happens to use a postgres-specific column type.
        _detect_dialect() sniffs column type modules, so a Postgres table built
        from generic types is treated as SQLite and keeps its constraints."""
        meta = _build_metadata(postgres_typed=postgres_typed)
        sandbox = create_sandbox_engine()
        try:
            sb_meta = clone_schema_to_sandbox(meta, sandbox)
            assert _constraint_kinds(sb_meta.tables["users"]) == expected_constraints
            with sandbox.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO users (id,user_id,email,plan,age) "
                        "VALUES (1,42,'a@b.com','pro',30)"
                    )
                )
                conn.commit()
            assert (
                _try_sql(
                    sandbox,
                    "INSERT INTO users (id,user_id,email,plan,age) "
                    "VALUES (2,42,'a@b.com','pro',1)",
                )
                == duplicate_email_result
            )
        finally:
            sandbox.dispose()

    # ---- What a correct implementation would do ----

    @pytest.mark.xfail(
        strict=True,
        reason="CLAIM 1 CONFIRMED: _clone_table_for_sqlite() drops UNIQUE, CHECK, "
        "FK and server defaults for every non-SQLite production database.",
    )
    def test_expected_pg_metadata_keeps_table_constraints(self):
        meta = _build_metadata(postgres_typed=True)
        sandbox = create_sandbox_engine()
        try:
            sb_meta = clone_schema_to_sandbox(meta, sandbox)
            assert "UniqueConstraint" in _constraint_kinds(sb_meta.tables["users"])
            assert "CheckConstraint" in _constraint_kinds(sb_meta.tables["users"])
            assert sb_meta.tables["users"].c.plan.server_default is not None
        finally:
            sandbox.dispose()


# ============================================================
# CLAIM 2 -- Lost update
#
# Code path: ShadowDB.__enter__ snapshots at clone time;
# ShadowDB.commit_to_production() -> ShadowDB.diff() diffs against that
# snapshot; sync.apply_changeset() issues UPDATE ... SET <every column>
# WHERE pk AND tenant, with no check that the row still looks as cloned.
# ============================================================


class TestClaim2LostUpdate:
    def test_observed_concurrent_write_is_silently_overwritten(self, tmp_path):
        engine, url = _prod_engine(tmp_path, "lost.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            # The agent touches one column.
            sandbox.execute("UPDATE tasks SET status = 'done' WHERE id = 1")

            # Meanwhile another process edits a DIFFERENT column of the same row.
            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE tasks SET title = 'Ship v2 [renamed by human]' "
                        "WHERE id = 1"
                    )
                )
            with other.connect() as conn:
                assert (
                    conn.execute(text("SELECT title FROM tasks WHERE id=1")).scalar_one()
                    == "Ship v2 [renamed by human]"
                )

            changeset = sandbox.diff()
            (diff,) = changeset.diffs
            # The diff carries the whole row as it looked at clone time, and it
            # reports only `status` as changed -- the agent never touched title.
            assert diff.changed_columns() == ["status"]
            assert diff.new["title"] == "Ship v2"
            assert diff.old["title"] == "Ship v2"

            # No warning, no conflict error.
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            title, status = conn.execute(
                text("SELECT title, status FROM tasks WHERE id=1")
            ).one()
        # The human's rename is gone, reverted to the clone-time value.
        assert title == "Ship v2"
        assert status == "done"

    def test_observed_commit_count_overstates_rows_written(self, tmp_path):
        """apply_changeset() does ``affected += 1`` per diff without looking at
        rowcount, so a statement that matched nothing still counts."""
        engine, url = _prod_engine(tmp_path, "vanished.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET status = 'done' WHERE id = 1")

            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(text("DELETE FROM tasks WHERE id = 1"))

            # Reports one row affected; zero rows were actually written.
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one() == 0

    @pytest.mark.xfail(
        strict=True,
        reason="CLAIM 2 CONFIRMED: no optimistic-concurrency check -- the commit "
        "overwrites every column from the clone-time snapshot.",
    )
    def test_expected_concurrent_write_is_detected_or_preserved(self, tmp_path):
        engine, url = _prod_engine(tmp_path, "lost_expected.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE tasks SET title = 'Ship v2 [renamed by human]' "
                        "WHERE id = 1"
                    )
                )
            try:
                sandbox.commit_to_production()
            except SyncError:
                return  # raising a conflict error is an acceptable outcome too

        with engine.connect() as conn:
            title = conn.execute(text("SELECT title FROM tasks WHERE id=1")).scalar_one()
        assert title == "Ship v2 [renamed by human]"


# ============================================================
# CLAIM 3 -- Validator inconsistency
#
# Code path: diff.RowDiff.validate() returns (True, "No validator") when
# models.get_validator() is None; sync.apply_changeset() calls
# models.validate_row(), which raises KeyError in exactly that case.
# ============================================================


class TestClaim3ValidatorInconsistency:
    def test_observed_diff_says_safe_but_commit_raises_keyerror(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "novalidator.db", TASKS_DDL)
        assert _model_registry == {}  # no SafeModel registered for "tasks"

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET status = 'archived' WHERE id = 1")
            changeset = sandbox.diff()

            # The dashboard side: green light.
            (diff,) = changeset.diffs
            assert diff.validate() == (True, "No validator")
            assert changeset.is_valid is True
            assert "[SAFE] AI CHANGES VERIFIED" in changeset._render_plain()

            # The sync side: hard failure, and not one of the documented types.
            with pytest.raises(KeyError) as excinfo:
                sandbox.commit_to_production()

            assert "No SafeModel registered for table 'tasks'" in str(excinfo.value)
            assert not isinstance(excinfo.value, SyncError)

        # The transaction rolled back, so production is untouched.
        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT status FROM tasks WHERE id=1")).scalar_one()
                == "todo"
            )

    def test_observed_both_call_sites_disagree_on_the_same_input(self):
        """Same table name, same row, opposite answers."""
        from safeagentdb.diff import DiffType, RowDiff
        from safeagentdb.models import validate_row

        row = {"id": 1, "user_id": 42, "title": "T", "status": "todo"}
        diff = RowDiff(
            table="unregistered",
            diff_type=DiffType.UPDATE,
            pk={"id": 1},
            old=row,
            new=row,
        )

        assert diff.validate() == (True, "No validator")
        with pytest.raises(KeyError):
            validate_row("unregistered", row)

    @pytest.mark.xfail(
        strict=True,
        reason="CLAIM 3 CONFIRMED: RowDiff.validate() passes an unregistered table, "
        "sync.validate_row() raises KeyError on the same table.",
    )
    def test_expected_missing_validator_handled_consistently(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "novalidator_expected.db", TASKS_DDL)

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET status = 'archived' WHERE id = 1")
            changeset = sandbox.diff()
            diff_says_ok = changeset.is_valid

            try:
                sandbox.commit_to_production()
                commit_ok = True
            except (SyncError, KeyError):
                commit_ok = False

        # Either both accept it or both reject it -- never one and then the other.
        assert diff_says_ok == commit_ok


# ============================================================
# CLAIM 4 (found while reading) -- Tables without a primary key
#
# Code path: ShadowDB._pk_columns() returns [] for a PK-less table;
# diff._pk_key() then returns the empty tuple for EVERY row, so
# diff.compute_diff() collapses the whole table into a single dict entry; and
# sync.apply_changeset() builds ``update(table)`` / ``delete(table)`` from an
# empty ``diff.pk``, leaving only the tenant predicate in the WHERE clause.
# ============================================================


class TestClaim4TablesWithoutPrimaryKey:
    def test_observed_every_row_collapses_to_one_diff_key(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "nopk_keys.db", EVENTS_DDL)
        _register_event_validator()

        with ShadowDB(engine, tables=["events"], tenant_id=42) as sandbox:
            assert sandbox._pk_columns() == {"events": []}
            # Three tenant rows are cloned...
            assert len(sandbox.query("SELECT * FROM events")) == 3
            # ...but the diff engine keys them all on the empty tuple.
            from safeagentdb.diff import _pk_key

            rows = sandbox._original_snapshot["events"]
            assert [_pk_key(r, []) for r in rows] == [(), (), ()]

    def test_observed_edit_to_one_row_overwrites_all_tenant_rows(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "nopk_update.db", EVENTS_DDL)
        _register_event_validator()

        with ShadowDB(engine, tables=["events"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE events SET payload='EDITED' WHERE kind='logout'")
            changeset = sandbox.diff()

            # The dashboard shows a single, clean, validated one-row update.
            assert changeset.summary == {"INSERT": 0, "UPDATE": 1, "DELETE": 0}
            (diff,) = changeset.diffs
            assert diff.pk == {}
            assert diff.new == {"user_id": 42, "kind": "logout", "payload": "EDITED"}
            assert changeset.is_valid is True

            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT user_id, kind, payload FROM events")).fetchall()

        # All three tenant-42 rows were flattened into copies of the edited row:
        # the UPDATE ran as `SET ... WHERE user_id = 42`, with no pk predicate.
        assert sorted(rows) == [
            (42, "logout", "EDITED"),
            (42, "logout", "EDITED"),
            (42, "logout", "EDITED"),
            (99, "login", "other-tenant"),
        ]

    def test_observed_delete_on_pkless_table_also_mass_overwrites(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "nopk_delete.db", EVENTS_DDL)
        _register_event_validator()

        with ShadowDB(engine, tables=["events"], tenant_id=42) as sandbox:
            sandbox.execute("DELETE FROM events WHERE kind='logout'")
            changeset = sandbox.diff()
            # A DELETE in the sandbox is not even reported as a DELETE.
            assert changeset.summary == {"INSERT": 0, "UPDATE": 1, "DELETE": 0}
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT user_id, kind, payload FROM events")).fetchall()
        assert sorted(rows) == [
            (42, "click", "b"),
            (42, "click", "b"),
            (42, "click", "b"),
            (99, "login", "other-tenant"),
        ]

    def test_observed_edit_to_a_non_last_row_is_silently_dropped(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "nopk_dropped.db", EVENTS_DDL)
        _register_event_validator()

        with ShadowDB(engine, tables=["events"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE events SET payload='EDITED' WHERE kind='login'")
            changeset = sandbox.diff()
            # Only the last row per table survives the collapse, so this change
            # is invisible to the diff and never reaches production.
            assert changeset.is_empty
            assert sandbox.commit_to_production() == 0

        with engine.connect() as conn:
            payload = conn.execute(
                text("SELECT payload FROM events WHERE kind='login' AND user_id=42")
            ).scalar_one()
        assert payload == "a"

    @pytest.mark.xfail(
        strict=True,
        reason="CLAIM 4 CONFIRMED: with no primary key, _pk_key() collapses the "
        "table to one row and apply_changeset() emits an UPDATE whose only "
        "predicate is the tenant column.",
    )
    def test_expected_pkless_table_is_rejected_or_handled_row_wise(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "nopk_expected.db", EVENTS_DDL)
        _register_event_validator()

        try:
            with ShadowDB(engine, tables=["events"], tenant_id=42) as sandbox:
                sandbox.execute("UPDATE events SET payload='EDITED' WHERE kind='logout'")
                sandbox.commit_to_production()
        except SyncError:
            return  # refusing a PK-less table outright is an acceptable outcome

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT user_id, kind, payload FROM events")).fetchall()
        assert sorted(rows) == [
            (42, "click", "b"),
            (42, "login", "a"),
            (42, "logout", "EDITED"),
            (99, "login", "other-tenant"),
        ]
