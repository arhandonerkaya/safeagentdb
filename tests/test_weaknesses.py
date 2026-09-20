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

from safeagentdb import ConflictError, SafeModel, SchemaError, ShadowDB, SyncError
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
    """Fixed in 0.2.0: the clone starts from Table.to_metadata() for every
    dialect, so constraints, unique indexes and defaults survive; only the parts
    SQLite genuinely cannot express are rewritten, and those are recorded."""

    def test_pg_metadata_keeps_unique_check_fk_and_default(self):
        meta = _build_metadata(postgres_typed=True)
        assert _detect_dialect(meta) == "postgresql"

        sandbox = create_sandbox_engine()
        try:
            sb_meta = clone_schema_to_sandbox(meta, sandbox)

            assert "UniqueConstraint" in _constraint_kinds(sb_meta.tables["users"])
            assert "CheckConstraint" in _constraint_kinds(sb_meta.tables["users"])
            assert "ForeignKeyConstraint" in _constraint_kinds(sb_meta.tables["tasks"])
            assert sb_meta.tables["users"].c.plan.server_default is not None
            assert sb_meta.tables["tasks"].c.owner_id.foreign_keys != set()

            # The JSONB column is still mapped to a SQLite-safe type.
            assert type(sb_meta.tables["users"].c.prefs.type).__name__ == "Text"

            ddl = _sandbox_ddl(sandbox)
            assert "UNIQUE" in ddl
            assert "CHECK" in ddl
            assert "FOREIGN KEY" in ddl
            assert "DEFAULT" in ddl
        finally:
            sandbox.dispose()

    def test_pg_sandbox_rejects_data_production_would_reject(self):
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
                == "IntegrityError"
            )

            # 2. value that violates the CHECK constraint
            assert (
                _try_sql(
                    sandbox,
                    "INSERT INTO users (id,user_id,email,plan,age) "
                    "VALUES (3,42,'c@d.com','pro',-5)",
                )
                == "IntegrityError"
            )

            # 3. foreign key pointing at a row that does not exist
            assert (
                _try_sql(
                    sandbox,
                    "INSERT INTO tasks (id,user_id,owner_id,title) "
                    "VALUES (1,42,999,'orphan')",
                )
                == "IntegrityError"
            )

            with sandbox.connect() as conn:
                assert conn.execute(text("SELECT COUNT(*) FROM users")).scalar_one() == 1
                assert conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one() == 0
        finally:
            sandbox.dispose()

    def test_pg_sandbox_accepts_an_insert_that_relies_on_a_server_default(self):
        """The server default is carried across, so an INSERT that omits a
        defaulted NOT NULL column succeeds in the sandbox as it would in
        production."""
        meta = _build_metadata(postgres_typed=True)
        sandbox = create_sandbox_engine()
        try:
            clone_schema_to_sandbox(meta, sandbox)
            assert (
                _try_sql(
                    sandbox,
                    "INSERT INTO users (id,user_id,email,age) VALUES (4,42,'e@f.com',1)",
                )
                == "ACCEPTED"
            )
            with sandbox.connect() as conn:
                plan = conn.execute(text("SELECT plan FROM users WHERE id=4")).scalar_one()
            assert plan == "free"
        finally:
            sandbox.dispose()

    def test_sqlite_production_preserves_constraints(self, tmp_path):
        """The SQLite path never lost constraints; it still does not."""
        engine, _ = _prod_engine(
            tmp_path,
            "constraints.db",
            [
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
        sandbox = create_sandbox_engine()
        try:
            sb_meta = clone_schema_to_sandbox(meta, sandbox)

            assert "UniqueConstraint" in _constraint_kinds(sb_meta.tables["users"])
            assert "CheckConstraint" in _constraint_kinds(sb_meta.tables["users"])
            assert "ForeignKeyConstraint" in _constraint_kinds(sb_meta.tables["tasks"])

            ddl = _sandbox_ddl(sandbox)
            assert "UNIQUE (email)" in ddl
            assert "CHECK (age >= 0)" in ddl
            assert "FOREIGN KEY(owner_id) REFERENCES users (id)" in ddl
            assert "DEFAULT 'free'" in ddl
            assert "CREATE UNIQUE INDEX ux_users_email" in ddl

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
            with sandbox.connect() as conn:
                conn.execute(
                    text("INSERT INTO users (id,user_id,email) VALUES (4,42,'e@f.com')")
                )
                conn.commit()
                plan = conn.execute(text("SELECT plan FROM users WHERE id=4")).scalar_one()
            assert plan == "free"
        finally:
            sandbox.dispose()

    def test_sandbox_enforces_foreign_keys(self, tmp_path):
        """PRAGMA foreign_keys is now switched on for every sandbox connection."""
        engine, _ = _prod_engine(
            tmp_path,
            "fk.db",
            [
                "CREATE TABLE users (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
                "CREATE TABLE tasks (\n"
                " id INTEGER PRIMARY KEY,\n"
                " user_id INTEGER NOT NULL,\n"
                " owner_id INTEGER NOT NULL REFERENCES users(id),\n"
                " title TEXT NOT NULL\n"
                ")",
            ],
        )
        meta = reflect_tables(engine, ["users", "tasks"])
        sandbox = create_sandbox_engine()
        try:
            clone_schema_to_sandbox(meta, sandbox)
            with sandbox.connect() as conn:
                assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
            assert (
                _try_sql(
                    sandbox,
                    "INSERT INTO tasks (id,user_id,owner_id,title) "
                    "VALUES (1,42,999,'orphan')",
                )
                == "IntegrityError"
            )
        finally:
            sandbox.dispose()

    @pytest.mark.parametrize("postgres_typed", [True, False])
    def test_fidelity_no_longer_depends_on_one_column_type(self, postgres_typed):
        """The same logical schema now produces the same sandbox whether or not
        it happens to use a postgres-specific column type."""
        meta = _build_metadata(postgres_typed=postgres_typed)
        sandbox = create_sandbox_engine()
        try:
            sb_meta = clone_schema_to_sandbox(meta, sandbox)
            assert _constraint_kinds(sb_meta.tables["users"]) == {
                "PrimaryKeyConstraint",
                "UniqueConstraint",
                "CheckConstraint",
            }
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
                == "IntegrityError"
            )
        finally:
            sandbox.dispose()

    def test_malformed_check_is_recorded_instead_of_crashing(self, tmp_path):
        """SQLAlchemy's SQLite CHECK reflection swallows the table's closing
        paren when it sits on the same line. That constraint is now skipped and
        listed in unsupported_constraints instead of raising OperationalError."""
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
        assert check.sqltext.text == "age >= 0)"  # still unbalanced upstream

        with ShadowDB(engine, tables=["users"], tenant_id=42) as sandbox:
            assert sandbox.query("SELECT * FROM users") == [
                {"id": 1, "user_id": 42, "age": 30}
            ]
            reported = sandbox.unsupported_constraints
            assert len(reported) == 1
            assert "CHECK" in reported[0]
            assert "users" in reported[0]


# ============================================================
# CLAIM 2 -- Lost update
#
# Code path: ShadowDB.__enter__ snapshots at clone time;
# ShadowDB.commit_to_production() -> ShadowDB.diff() diffs against that
# snapshot; sync.apply_changeset() issues UPDATE ... SET <every column>
# WHERE pk AND tenant, with no check that the row still looks as cloned.
# ============================================================


class TestClaim2LostUpdate:
    """Fixed in 0.2.0: every UPDATE/DELETE target is re-read and compared
    against the clone-time values, only changed columns are written, and the
    affected count comes from each statement's rowcount."""

    def test_concurrent_write_raises_conflict_error(self, tmp_path):
        engine, url = _prod_engine(tmp_path, "lost.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET status = 'done' WHERE id = 1")

            # Another process edits a DIFFERENT column of the same row.
            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE tasks SET title = 'Ship v2 [renamed by human]' "
                        "WHERE id = 1"
                    )
                )

            with pytest.raises(ConflictError) as excinfo:
                sandbox.commit_to_production()

        error = excinfo.value
        assert error.table == "tasks"
        assert error.row_key == {"id": 1}
        assert error.columns == ["title"]
        assert isinstance(error, SyncError)

        # The whole changeset rolled back: the human's rename survives and the
        # agent's status change was not applied.
        with engine.connect() as conn:
            title, status = conn.execute(
                text("SELECT title, status FROM tasks WHERE id=1")
            ).one()
        assert title == "Ship v2 [renamed by human]"
        assert status == "todo"

    def test_update_writes_only_changed_columns(self, tmp_path):
        """A column the agent never touched is not part of the UPDATE, so a
        concurrent edit to a different column of a different row is untouched
        and the write is minimal."""
        engine, _ = _prod_engine(tmp_path, "minimal.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
            changeset = sandbox.diff()
            (diff,) = changeset.diffs
            assert diff.changed_columns() == ["status"]
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            title, status = conn.execute(
                text("SELECT title, status FROM tasks WHERE id=1")
            ).one()
        assert title == "Ship v2"
        assert status == "done"

    def test_on_conflict_ignore_skips_the_drifted_row(self, tmp_path):
        engine, url = _prod_engine(
            tmp_path,
            "ignore.db",
            TASKS_DDL + ["INSERT INTO tasks VALUES (2, 42, 'Write docs', 'todo')"],
        )
        _register_task_validator()

        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, on_conflict="ignore"
        ) as sandbox:
            sandbox.execute("UPDATE tasks SET status = 'done' WHERE id IN (1, 2)")

            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(text("UPDATE tasks SET title = 'renamed' WHERE id = 1"))

            # Row 1 drifted and is skipped; row 2 still applies.
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, title, status FROM tasks ORDER BY id")
            ).fetchall()
        assert rows == [
            (1, "renamed", "todo"),
            (2, "Write docs", "done"),
        ]

    def test_vanished_row_raises_conflict_error(self, tmp_path):
        engine, url = _prod_engine(tmp_path, "vanished.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET status = 'done' WHERE id = 1")

            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(text("DELETE FROM tasks WHERE id = 1"))

            with pytest.raises(ConflictError, match="deleted in production"):
                sandbox.commit_to_production()

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one() == 0

    def test_commit_count_is_the_real_rowcount(self, tmp_path):
        engine, _ = _prod_engine(
            tmp_path,
            "count.db",
            TASKS_DDL
            + [
                "INSERT INTO tasks VALUES (2, 42, 'Write docs', 'todo')",
                "INSERT INTO tasks VALUES (3, 42, 'Fix bug', 'todo')",
            ],
        )
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
            sandbox.execute("DELETE FROM tasks WHERE id = 2")
            sandbox.execute(
                "INSERT INTO tasks (id, user_id, title, status) "
                "VALUES (4, 42, 'New task', 'todo')"
            )
            assert sandbox.commit_to_production() == 3

        with engine.connect() as conn:
            ids = [r[0] for r in conn.execute(text("SELECT id FROM tasks ORDER BY id"))]
        assert ids == [1, 3, 4]


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
    """Fixed in 0.2.0: a table with no primary key is rejected unless the caller
    supplies an explicit row_key, and apply_changeset refuses to execute an
    UPDATE/DELETE that has no row-identifying predicate."""

    def test_pkless_table_is_rejected_with_an_actionable_error(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "nopk_reject.db", EVENTS_DDL)
        _register_event_validator()

        with pytest.raises(SchemaError) as excinfo:
            with ShadowDB(engine, tables=["events"], tenant_id=42):
                pass

        message = str(excinfo.value)
        assert "events" in message
        assert "no primary key" in message
        assert "row_key" in message

    def test_explicit_row_key_identifies_rows(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "nopk_rowkey.db", EVENTS_DDL)
        _register_event_validator()

        with ShadowDB(
            engine,
            tables=["events"],
            tenant_id=42,
            row_key={"events": ["user_id", "kind"]},
        ) as sandbox:
            assert sandbox.row_keys == {"events": ["user_id", "kind"]}

            sandbox.execute("UPDATE events SET payload='EDITED' WHERE kind='logout'")
            changeset = sandbox.diff()

            (diff,) = changeset.diffs
            assert diff.pk == {"user_id": 42, "kind": "logout"}
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT user_id, kind, payload FROM events")).fetchall()
        # Exactly one row changed; the other two are intact.
        assert sorted(rows) == [
            (42, "click", "b"),
            (42, "login", "a"),
            (42, "logout", "EDITED"),
            (99, "login", "other-tenant"),
        ]

    def test_edit_to_a_non_last_row_is_no_longer_dropped(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "nopk_nonlast.db", EVENTS_DDL)
        _register_event_validator()

        with ShadowDB(
            engine,
            tables=["events"],
            tenant_id=42,
            row_key={"events": ["user_id", "kind"]},
        ) as sandbox:
            sandbox.execute("UPDATE events SET payload='EDITED' WHERE kind='login'")
            changeset = sandbox.diff()
            assert not changeset.is_empty
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            payload = conn.execute(
                text("SELECT payload FROM events WHERE kind='login' AND user_id=42")
            ).scalar_one()
        assert payload == "EDITED"

    def test_delete_removes_only_the_targeted_row(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "nopk_del.db", EVENTS_DDL)
        _register_event_validator()

        with ShadowDB(
            engine,
            tables=["events"],
            tenant_id=42,
            row_key={"events": ["user_id", "kind"]},
        ) as sandbox:
            sandbox.execute("DELETE FROM events WHERE kind='logout'")
            changeset = sandbox.diff()
            assert changeset.summary == {"INSERT": 0, "UPDATE": 0, "DELETE": 1}
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT user_id, kind FROM events")).fetchall()
        assert sorted(rows) == [
            (42, "click"),
            (42, "login"),
            (99, "login"),
        ]

    def test_row_key_naming_a_missing_column_is_rejected(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "nopk_badcol.db", EVENTS_DDL)
        _register_event_validator()

        with pytest.raises(SchemaError, match="do not exist"):
            with ShadowDB(
                engine,
                tables=["events"],
                tenant_id=42,
                row_key={"events": ["user_id", "nope"]},
            ):
                pass

    def test_non_unique_row_key_is_rejected(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "nopk_dupe.db", EVENTS_DDL)
        _register_event_validator()

        # 'user_id' alone repeats across the three cloned rows.
        with pytest.raises(SchemaError, match="not unique"):
            with ShadowDB(
                engine,
                tables=["events"],
                tenant_id=42,
                row_key={"events": ["user_id"]},
            ):
                pass

    def test_apply_changeset_refuses_an_update_with_no_row_key(self, tmp_path):
        """The hard guard, exercised directly: even if a changeset reaches
        sync.apply_changeset carrying an empty key, nothing is executed."""
        from safeagentdb.diff import ChangeSet, DiffType, RowDiff
        from safeagentdb.engine import reflect_tables
        from safeagentdb.sync import apply_changeset

        engine, _ = _prod_engine(tmp_path, "nopk_guard.db", EVENTS_DDL)
        _register_event_validator()
        meta = reflect_tables(engine, ["events"])

        changeset = ChangeSet(
            diffs=[
                RowDiff(
                    table="events",
                    diff_type=DiffType.UPDATE,
                    pk={},
                    old={"user_id": 42, "kind": "logout", "payload": "c"},
                    new={"user_id": 42, "kind": "logout", "payload": "EDITED"},
                )
            ]
        )

        with pytest.raises(SyncError, match="without a row-identifying key"):
            apply_changeset(engine, meta, changeset, "user_id", 42)

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT user_id, kind, payload FROM events")).fetchall()
        assert sorted(rows) == [
            (42, "click", "b"),
            (42, "login", "a"),
            (42, "logout", "c"),
            (99, "login", "other-tenant"),
        ]

    def test_apply_changeset_rejects_a_key_that_is_not_the_declared_row_key(self, tmp_path):
        from safeagentdb.diff import ChangeSet, DiffType, RowDiff
        from safeagentdb.engine import reflect_tables
        from safeagentdb.sync import apply_changeset

        engine, _ = _prod_engine(tmp_path, "nopk_mismatch.db", EVENTS_DDL)
        _register_event_validator()
        meta = reflect_tables(engine, ["events"])

        changeset = ChangeSet(
            diffs=[
                RowDiff(
                    table="events",
                    diff_type=DiffType.UPDATE,
                    pk={"user_id": 42},
                    old={"user_id": 42, "kind": "logout", "payload": "c"},
                    new={"user_id": 42, "kind": "logout", "payload": "EDITED"},
                )
            ]
        )

        with pytest.raises(SyncError, match="Row key mismatch"):
            apply_changeset(
                engine,
                meta,
                changeset,
                "user_id",
                42,
                row_keys={"events": ["user_id", "kind"]},
            )
