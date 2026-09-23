"""
test_audit_followups.py -- The five issues found reviewing the 0.2.0 hardening
branch, each fixed and pinned here.

1. Foreign keys vs tenant cloning -- a parent table that cloned zero rows made
   PRAGMA foreign_keys=ON reject legitimate inserts.
2. Generated columns -- a dropped nextval() default let the sandbox invent a
   primary key that production's sequence never issued.
3. Database errors -- IntegrityError escaped the SafeAgentDBError hierarchy.
4. Conflict detection -- the re-read left a window open between the check and
   the write.
5. on_conflict="ignore" -- a partial apply with no record of what was skipped.

Plain pytest, file-based SQLite in tmp_path, postgresql-typed MetaData built in
memory. No server, no network, no Docker.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from safeagentdb import (
    ConflictError,
    GeneratedValueError,
    IntegrityViolationError,
    SafeAgentDBError,
    SafeModel,
    ShadowDB,
    SyncError,
)
from safeagentdb.models import _model_registry


@pytest.fixture(autouse=True)
def _isolated_registry():
    saved = dict(_model_registry)
    _model_registry.clear()
    yield
    _model_registry.clear()
    _model_registry.update(saved)


def _prod(tmp_path, name: str, ddl: list[str]):
    url = f"sqlite:///{tmp_path / name}"
    engine = create_engine(url)
    with engine.begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))
    return engine, url


# A shared lookup table with no tenant column, and a tenant table pointing at it.
LOOKUP_DDL = [
    "CREATE TABLE statuses (\n id INTEGER PRIMARY KEY,\n label TEXT NOT NULL\n)",
    "CREATE TABLE tasks (\n"
    " id INTEGER PRIMARY KEY,\n"
    " user_id INTEGER NOT NULL,\n"
    " status_id INTEGER NOT NULL REFERENCES statuses(id),\n"
    " title TEXT NOT NULL\n"
    ")",
    "INSERT INTO statuses VALUES (1, 'todo'), (2, 'done')",
    "INSERT INTO tasks VALUES (10, 42, 1, 'existing')",
]


def _register_lookup_validators():
    class StatusValidator(SafeModel):
        __table_name__ = "statuses"
        id: int
        label: str

    class TaskValidator(SafeModel):
        __table_name__ = "tasks"
        id: int
        user_id: int
        status_id: int
        title: str

    return StatusValidator, TaskValidator


# ============================================================
# 1. Foreign keys pointing at a table that cloned no rows
# ============================================================


class TestForeignKeysVsTenantCloning:
    def test_fk_to_an_empty_parent_is_not_enforced_and_is_reported(self, tmp_path):
        """The regression: statuses has no tenant column, so it clones zero rows,
        and every task.status_id then looked dangling."""
        engine, _ = _prod(tmp_path, "lookup.db", LOOKUP_DDL)
        _register_lookup_validators()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            assert sandbox.clone_stats["statuses"] == 0

            # status_id=2 exists in production. This must be accepted.
            sandbox.execute(
                "INSERT INTO tasks (id, user_id, status_id, title) "
                "VALUES (11, 42, 2, 'legitimate new task')"
            )
            sandbox.session.commit()

            reported = sandbox.unsupported_constraints
            assert any("statuses" in item and "FOREIGN KEY" in item for item in reported)
            assert any("cloned 0 rows" in item for item in reported)
            assert any("reference_tables" in item for item in reported)

            # The warning reaches the diff output the reviewer reads.
            plain = sandbox.diff()._render_plain()
            assert "[NOT ENFORCED IN SANDBOX]" in plain
            assert "statuses" in plain

            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, status_id FROM tasks ORDER BY id")).fetchall()
        assert rows == [(10, 1), (11, 2)]

    def test_reference_tables_clones_the_lookup_and_enforces_the_fk(self, tmp_path):
        """With the lookup table listed, the FK is real again."""
        engine, _ = _prod(tmp_path, "lookup_ref.db", LOOKUP_DDL)
        _register_lookup_validators()

        with ShadowDB(
            engine,
            tables=["tasks"],
            tenant_id=42,
            reference_tables=["statuses"],
        ) as sandbox:
            assert sandbox.clone_stats["statuses"] == 2
            assert sandbox.reference_table_names == ["statuses"]
            # Nothing had to be given up this time.
            assert sandbox.unsupported_constraints == []

            # The legitimate insert still works...
            sandbox.execute(
                "INSERT INTO tasks (id, user_id, status_id, title) "
                "VALUES (11, 42, 2, 'legitimate new task')"
            )
            sandbox.session.commit()

            # ...and a genuinely dangling reference is now caught in the sandbox.
            with pytest.raises(IntegrityError):
                sandbox.execute(
                    "INSERT INTO tasks (id, user_id, status_id, title) "
                    "VALUES (12, 42, 999, 'bad reference')"
                )
                sandbox.session.commit()
            sandbox.session.rollback()

            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, status_id FROM tasks ORDER BY id")).fetchall()
        assert rows == [(10, 1), (11, 2)]

    def test_reference_table_rows_are_visible_to_the_agent(self, tmp_path):
        engine, _ = _prod(tmp_path, "lookup_visible.db", LOOKUP_DDL)
        _register_lookup_validators()

        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, reference_tables=["statuses"]
        ) as sandbox:
            assert sandbox.query("SELECT label FROM statuses ORDER BY id") == [
                {"label": "todo"},
                {"label": "done"},
            ]

    def test_reference_tables_are_read_only(self, tmp_path):
        engine, _ = _prod(tmp_path, "lookup_ro.db", LOOKUP_DDL)
        _register_lookup_validators()

        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, reference_tables=["statuses"]
        ) as sandbox:
            sandbox.execute("UPDATE statuses SET label='CHANGED' WHERE id=1")

            with pytest.raises(SyncError, match="read-only"):
                sandbox.diff()

        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT label FROM statuses WHERE id=1")).scalar_one()
                == "todo"
            )

    def test_reference_table_changes_are_not_part_of_the_changeset(self, tmp_path):
        """An untouched reference table simply never appears in the diff."""
        engine, _ = _prod(tmp_path, "lookup_absent.db", LOOKUP_DDL)
        _register_lookup_validators()

        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, reference_tables=["statuses"]
        ) as sandbox:
            sandbox.execute("UPDATE tasks SET title='renamed' WHERE id=10")
            changeset = sandbox.diff()
            assert {d.table for d in changeset.diffs} == {"tasks"}

    def test_a_tenant_with_no_rows_in_a_parent_also_disables_the_fk(self, tmp_path):
        """Not only tenant-less parents: a parent the tenant owns nothing in
        clones zero rows too, and would reject every child insert."""
        engine, _ = _prod(
            tmp_path,
            "empty_parent.db",
            [
                "CREATE TABLE projects (\n"
                " id INTEGER PRIMARY KEY,\n"
                " user_id INTEGER NOT NULL\n"
                ")",
                "CREATE TABLE tasks (\n"
                " id INTEGER PRIMARY KEY,\n"
                " user_id INTEGER NOT NULL,\n"
                " project_id INTEGER NOT NULL REFERENCES projects(id),\n"
                " title TEXT NOT NULL\n"
                ")",
                # Everything belongs to tenant 99.
                "INSERT INTO projects VALUES (1, 99)",
                "INSERT INTO tasks VALUES (10, 99, 1, 'theirs')",
            ],
        )

        class ProjectValidator(SafeModel):
            __table_name__ = "projects"
            id: int
            user_id: int

        class TaskValidator(SafeModel):
            __table_name__ = "tasks"
            id: int
            user_id: int
            project_id: int
            title: str

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            assert sandbox.clone_stats == {"projects": 0, "tasks": 0}
            sandbox.execute(
                "INSERT INTO tasks (id, user_id, project_id, title) "
                "VALUES (11, 42, 1, 'mine, existing project')"
            )
            sandbox.session.commit()
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT COUNT(*) FROM tasks WHERE user_id=42")
            ).scalar_one() == 1

    def test_a_populated_parent_still_enforces_its_fk(self, tmp_path):
        """The fix must not disable foreign keys wholesale."""
        engine, _ = _prod(
            tmp_path,
            "populated_parent.db",
            [
                "CREATE TABLE projects (\n"
                " id INTEGER PRIMARY KEY,\n"
                " user_id INTEGER NOT NULL\n"
                ")",
                "CREATE TABLE tasks (\n"
                " id INTEGER PRIMARY KEY,\n"
                " user_id INTEGER NOT NULL,\n"
                " project_id INTEGER NOT NULL REFERENCES projects(id),\n"
                " title TEXT NOT NULL\n"
                ")",
                "INSERT INTO projects VALUES (1, 42)",
                "INSERT INTO tasks VALUES (10, 42, 1, 'mine')",
            ],
        )

        class ProjectValidator(SafeModel):
            __table_name__ = "projects"
            id: int
            user_id: int

        class TaskValidator(SafeModel):
            __table_name__ = "tasks"
            id: int
            user_id: int
            project_id: int
            title: str

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            assert sandbox.clone_stats["projects"] == 1
            assert sandbox.unsupported_constraints == []
            with pytest.raises(IntegrityError):
                sandbox.execute(
                    "INSERT INTO tasks (id, user_id, project_id, title) "
                    "VALUES (11, 42, 999, 'dangling')"
                )
                sandbox.session.commit()


# ============================================================
# 2. Columns only production can generate
# ============================================================


SERIAL_DDL_COLLIDING = [
    "CREATE TABLE tasks (\n"
    " id INTEGER PRIMARY KEY,\n"
    " user_id INTEGER NOT NULL,\n"
    " title TEXT NOT NULL\n"
    ")",
    # Ids interleave across tenants: 2 and 4 belong to someone else.
    "INSERT INTO tasks VALUES (1, 42, 'mine a')",
    "INSERT INTO tasks VALUES (2, 99, 'theirs a')",
    "INSERT INTO tasks VALUES (3, 42, 'mine b')",
    "INSERT INTO tasks VALUES (4, 99, 'theirs b')",
]

SERIAL_DDL_GAP = [
    "CREATE TABLE tasks (\n"
    " id INTEGER PRIMARY KEY,\n"
    " user_id INTEGER NOT NULL,\n"
    " title TEXT NOT NULL\n"
    ")",
    # Production has a gap at 2 and is far ahead at 900.
    "INSERT INTO tasks VALUES (1, 42, 'mine a')",
    "INSERT INTO tasks VALUES (3, 42, 'mine b')",
    "INSERT INTO tasks VALUES (900, 99, 'theirs, far ahead')",
]


def _register_task_validator():
    class TaskValidator(SafeModel):
        __table_name__ = "tasks"
        id: int
        user_id: int
        title: str

    return TaskValidator


@pytest.fixture
def serial_metadata(monkeypatch):
    """Reflect 'tasks.id' with a Postgres-style nextval() default."""
    from sqlalchemy import DefaultClause

    import safeagentdb.sandbox as sandbox_module
    from safeagentdb.engine import reflect_tables as real_reflect

    def patched(source_engine, table_names):
        meta = real_reflect(source_engine, table_names)
        if "tasks" in meta.tables:
            meta.tables["tasks"].c.id.server_default = DefaultClause(
                text("nextval('tasks_id_seq'::regclass)")
            )
        return meta

    monkeypatch.setattr(sandbox_module, "reflect_tables", patched)
    return patched


class TestGeneratedColumns:
    def test_dropped_default_is_recorded_on_the_sandbox(self, tmp_path, serial_metadata):
        engine, _ = _prod(tmp_path, "serial_report.db", SERIAL_DDL_GAP)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            assert sandbox.generated_columns == {"tasks": ["id"]}
            assert any(
                "nextval" in item and "tasks.id" in item
                for item in sandbox.unsupported_constraints
            )

    def test_gap_case_is_refused_instead_of_silently_written(
        self, tmp_path, serial_metadata
    ):
        """The dangerous one: production has room at id 4, so 0.2.0 wrote a row
        with an id the sequence never issued."""
        engine, _ = _prod(tmp_path, "serial_gap.db", SERIAL_DDL_GAP)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("INSERT INTO tasks (user_id, title) VALUES (42, 'new row')")
            changeset = sandbox.diff()

            # The sandbox still invents 4 internally...
            (diff,) = changeset.diffs
            assert diff.new["id"] == 4

            # ...but the dashboard no longer calls it valid.
            assert changeset.is_valid is False
            ok, message = diff.validate()
            assert ok is False
            assert "generated by production" in message
            assert "[BLOCKED]" in changeset._render_plain()

            with pytest.raises(GeneratedValueError) as excinfo:
                sandbox.commit_to_production()
            assert excinfo.value.table == "tasks"
            assert excinfo.value.columns == ["id"]

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id FROM tasks ORDER BY id")).fetchall()
        assert rows == [(1,), (3,), (900,)]

    def test_collision_case_is_refused_before_the_database_sees_it(
        self, tmp_path, serial_metadata
    ):
        """0.2.0 got as far as a raw IntegrityError here. Now it never runs."""
        engine, _ = _prod(tmp_path, "serial_collide.db", SERIAL_DDL_COLLIDING)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("INSERT INTO tasks (user_id, title) VALUES (42, 'new row')")
            changeset = sandbox.diff()
            assert changeset.diffs[0].new["id"] == 4  # tenant 99 owns id 4
            assert changeset.is_valid is False

            with pytest.raises(GeneratedValueError):
                sandbox.commit_to_production()

        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, user_id FROM tasks ORDER BY id")
            ).fetchall()
        assert rows == [(1, 42), (2, 99), (3, 42), (4, 99)]

    def test_an_explicit_id_is_refused_too(self, tmp_path, serial_metadata):
        """An explicitly supplied key bypasses the production sequence just as
        badly, and after the fact it is indistinguishable from an invented one."""
        engine, _ = _prod(tmp_path, "serial_explicit.db", SERIAL_DDL_GAP)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute(
                "INSERT INTO tasks (id, user_id, title) VALUES (77, 42, 'explicit')"
            )
            assert sandbox.diff().is_valid is False
            with pytest.raises(GeneratedValueError):
                sandbox.commit_to_production()

    def test_updates_and_deletes_are_unaffected(self, tmp_path, serial_metadata):
        engine, _ = _prod(tmp_path, "serial_update.db", SERIAL_DDL_GAP)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET title='renamed' WHERE id=1")
            sandbox.execute("DELETE FROM tasks WHERE id=3")
            changeset = sandbox.diff()
            assert changeset.is_valid is True
            assert sandbox.commit_to_production() == 2

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, title FROM tasks ORDER BY id")).fetchall()
        assert rows == [(1, "renamed"), (900, "theirs, far ahead")]

    def test_a_nullable_generated_column_only_blocks_when_not_supplied(self, tmp_path):
        """A non-key generated column is distinguishable: NULL means the agent
        did not supply it, anything else means it did."""
        from sqlalchemy import DefaultClause

        import safeagentdb.sandbox as sandbox_module
        from safeagentdb.engine import reflect_tables as real_reflect

        engine, _ = _prod(
            tmp_path,
            "uuid_col.db",
            [
                "CREATE TABLE tasks (\n"
                " id INTEGER PRIMARY KEY,\n"
                " user_id INTEGER NOT NULL,\n"
                " public_ref TEXT,\n"
                " title TEXT NOT NULL\n"
                ")",
                "INSERT INTO tasks VALUES (1, 42, 'ref-1', 'existing')",
            ],
        )

        def patched(source_engine, table_names):
            meta = real_reflect(source_engine, table_names)
            meta.tables["tasks"].c.public_ref.server_default = DefaultClause(
                text("gen_random_uuid()")
            )
            return meta

        class TaskValidator(SafeModel):
            __table_name__ = "tasks"
            id: int
            user_id: int
            public_ref: str | None
            title: str

        import pytest as _pytest

        mp = _pytest.MonkeyPatch()
        mp.setattr(sandbox_module, "reflect_tables", patched)
        try:
            # Omitting the generated column is refused...
            with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
                assert sandbox.generated_columns == {"tasks": ["public_ref"]}
                sandbox.execute(
                    "INSERT INTO tasks (id, user_id, title) VALUES (2, 42, 'no ref')"
                )
                assert sandbox.diff().is_valid is False
                with pytest.raises(GeneratedValueError):
                    sandbox.commit_to_production()

            # ...but supplying it is fine, since id is not generated here.
            with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
                sandbox.execute(
                    "INSERT INTO tasks (id, user_id, public_ref, title) "
                    "VALUES (3, 42, 'ref-3', 'with ref')"
                )
                assert sandbox.diff().is_valid is True
                assert sandbox.commit_to_production() == 1
        finally:
            mp.undo()

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, public_ref FROM tasks ORDER BY id")).fetchall()
        assert rows == [(1, "ref-1"), (3, "ref-3")]


# ============================================================
# 3. Database errors stay inside the SafeAgentDBError hierarchy
# ============================================================


UNIQUE_DDL = [
    "CREATE TABLE users (\n"
    " id INTEGER PRIMARY KEY,\n"
    " user_id INTEGER NOT NULL,\n"
    " email TEXT NOT NULL UNIQUE\n"
    ")",
    "INSERT INTO users VALUES (1, 42, 'mine@example.com')",
    "INSERT INTO users VALUES (2, 99, 'taken@example.com')",
]


def _register_user_validator():
    class UserValidator(SafeModel):
        __table_name__ = "users"
        id: int
        user_id: int
        email: str

    return UserValidator


class TestDatabaseErrorsAreWrapped:
    def test_cross_tenant_unique_collision(self, tmp_path):
        """taken@example.com belongs to tenant 99 and was never cloned, so the
        sandbox cannot see the collision. Production does."""
        engine, _ = _prod(tmp_path, "unique_cross.db", UNIQUE_DDL)
        _register_user_validator()

        with ShadowDB(engine, tables=["users"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE users SET email='taken@example.com' WHERE id=1")
            assert sandbox.diff().is_valid is True  # the sandbox cannot know

            with pytest.raises(IntegrityViolationError) as excinfo:
                sandbox.commit_to_production()

        error = excinfo.value
        assert isinstance(error, SafeAgentDBError)
        assert isinstance(error, SyncError)
        assert error.table == "users"
        assert error.row_key == {"id": 1}
        assert isinstance(error.__cause__, IntegrityError)
        assert "only this tenant's rows" in str(error)

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, email FROM users ORDER BY id")).fetchall()
        assert rows == [(1, "mine@example.com"), (2, "taken@example.com")]

    def test_primary_key_collision_with_another_tenants_row(self, tmp_path):
        """The Q2 collision, on a table with no generated default, so it really
        does reach the database."""
        engine, _ = _prod(
            tmp_path,
            "pk_collide.db",
            [
                "CREATE TABLE tasks (\n"
                " id INTEGER PRIMARY KEY,\n"
                " user_id INTEGER NOT NULL,\n"
                " title TEXT NOT NULL\n"
                ")",
                "INSERT INTO tasks VALUES (1, 42, 'mine')",
                "INSERT INTO tasks VALUES (4, 99, 'theirs')",
            ],
        )
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            # id 4 is invisible here: it belongs to tenant 99.
            sandbox.execute("INSERT INTO tasks (id, user_id, title) VALUES (4, 42, 'clash')")
            assert sandbox.diff().is_valid is True

            with pytest.raises(IntegrityViolationError) as excinfo:
                sandbox.commit_to_production()

        assert excinfo.value.table == "tasks"
        assert excinfo.value.row_key == {"id": 4}
        assert isinstance(excinfo.value.__cause__, IntegrityError)

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, user_id FROM tasks ORDER BY id")).fetchall()
        assert rows == [(1, 42), (4, 99)]

    def test_a_single_except_catches_every_documented_failure(self, tmp_path):
        """The handler the README tells people to write must actually work."""
        engine, _ = _prod(tmp_path, "one_except.db", UNIQUE_DDL)
        _register_user_validator()

        with ShadowDB(engine, tables=["users"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE users SET email='taken@example.com' WHERE id=1")
            try:
                sandbox.commit_to_production()
                caught = None
            except SafeAgentDBError as exc:
                caught = exc

        assert isinstance(caught, IntegrityViolationError)


# ============================================================
# 4. Compare-and-swap conflict detection
# ============================================================


NULLABLE_DDL = [
    "CREATE TABLE tasks (\n"
    " id INTEGER PRIMARY KEY,\n"
    " user_id INTEGER NOT NULL,\n"
    " title TEXT NOT NULL,\n"
    " status TEXT NOT NULL,\n"
    " assignee TEXT,\n"
    " score REAL\n"
    ")",
    "INSERT INTO tasks VALUES (1, 42, 'a', 'todo', NULL, 1.5)",
    "INSERT INTO tasks VALUES (2, 42, 'b', 'todo', 'alice', 2.5)",
]


def _register_nullable_validator():
    class TaskValidator(SafeModel):
        __table_name__ = "tasks"
        id: int
        user_id: int
        title: str
        status: str
        assignee: str | None
        score: float | None

    return TaskValidator


def _guard_table():
    from sqlalchemy import Column, Float, Integer, MetaData, String, Table

    meta = MetaData()
    return Table(
        "tasks",
        meta,
        Column("id", Integer, primary_key=True),
        Column("user_id", Integer),
        Column("title", String),
        Column("status", String),
        Column("assignee", String),
        Column("score", Float),
    )


class TestCompareAndSwap:
    def test_the_guard_is_part_of_the_write(self):
        """The clone-time value goes into the WHERE clause, so there is no
        window between checking and writing."""
        from sqlalchemy import update

        from safeagentdb.diff import DiffType, RowDiff
        from safeagentdb.sync import _guard_columns, _matches

        table = _guard_table()
        old = {
            "id": 1,
            "user_id": 42,
            "title": "a",
            "status": "todo",
            "assignee": None,
            "score": 1.5,
        }
        diff = RowDiff(
            table="tasks",
            diff_type=DiffType.UPDATE,
            pk={"id": 1},
            old=old,
            new={**old, "status": "done"},
        )

        guarded = _guard_columns(diff, table, {"id": 1})
        assert guarded == ["status"]  # only what the agent changed

        stmt = update(table).where(_matches(table.c.id, 1))
        for col in guarded:
            stmt = stmt.where(_matches(table.c[col], old[col]))
        compiled = str(stmt.values(status="done").compile())
        assert "WHERE" in compiled
        assert compiled.count("status") >= 2  # both SET and WHERE

    def test_a_delete_guards_the_whole_row(self):
        from safeagentdb.diff import DiffType, RowDiff
        from safeagentdb.sync import _guard_columns

        table = _guard_table()
        old = {
            "id": 1,
            "user_id": 42,
            "title": "a",
            "status": "todo",
            "assignee": None,
            "score": 1.5,
        }
        diff = RowDiff(table="tasks", diff_type=DiffType.DELETE, pk={"id": 1}, old=old)

        guarded = _guard_columns(diff, table, {"id": 1})
        assert "status" in guarded
        assert "assignee" in guarded
        assert "title" in guarded
        assert "score" not in guarded  # float: unreliable equality
        assert "id" not in guarded  # already the row key

    def test_concurrent_update_to_the_guarded_column_conflicts(self, tmp_path):
        engine, url = _prod(tmp_path, "cas_update.db", NULLABLE_DDL)
        _register_nullable_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET status='done' WHERE id=1")

            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(text("UPDATE tasks SET status='blocked' WHERE id=1"))

            with pytest.raises(ConflictError) as excinfo:
                sandbox.commit_to_production()
            assert excinfo.value.columns == ["status"]

        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT status FROM tasks WHERE id=1")).scalar_one()
                == "blocked"
            )

    def test_concurrent_delete_conflicts(self, tmp_path):
        engine, url = _prod(tmp_path, "cas_delete.db", NULLABLE_DDL)
        _register_nullable_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET status='done' WHERE id=1")

            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(text("DELETE FROM tasks WHERE id=1"))

            with pytest.raises(ConflictError, match="deleted in production"):
                sandbox.commit_to_production()

    def test_agent_delete_of_a_drifted_row_conflicts(self, tmp_path):
        """A DELETE guards every comparable column, so removing a row somebody
        else just edited is a conflict, not a silent loss."""
        engine, url = _prod(tmp_path, "cas_del_drift.db", NULLABLE_DDL)
        _register_nullable_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("DELETE FROM tasks WHERE id=2")

            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(text("UPDATE tasks SET title='edited by human' WHERE id=2"))

            with pytest.raises(ConflictError) as excinfo:
                sandbox.commit_to_production()
            assert excinfo.value.columns == ["title"]

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one() == 2

    def test_a_column_that_was_null_at_clone_time(self, tmp_path):
        """'col = NULL' never matches, so a NULL guard must become 'col IS NULL'."""
        engine, _ = _prod(tmp_path, "cas_null_clone.db", NULLABLE_DDL)
        _register_nullable_validator()

        # assignee is NULL on row 1 at clone time and stays NULL: must apply.
        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET assignee='bob' WHERE id=1")
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT assignee FROM tasks WHERE id=1")).scalar_one()
                == "bob"
            )

        # Now the same column drifts away from NULL between clone and commit.
        engine2, url2 = _prod(tmp_path, "cas_null_drift.db", NULLABLE_DDL)
        with ShadowDB(engine2, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET assignee='bob' WHERE id=1")

            other = create_engine(url2)
            with other.begin() as conn:
                conn.execute(text("UPDATE tasks SET assignee='carol' WHERE id=1"))

            with pytest.raises(ConflictError) as excinfo:
                sandbox.commit_to_production()
            assert excinfo.value.columns == ["assignee"]

        with engine2.connect() as conn:
            assert (
                conn.execute(text("SELECT assignee FROM tasks WHERE id=1")).scalar_one()
                == "carol"
            )

    def test_a_column_that_becomes_null(self, tmp_path):
        """Clone-time value is non-NULL; production sets it to NULL. The guard
        'col = value' is NULL against a NULL column, so it must not match."""
        engine, url = _prod(tmp_path, "cas_becomes_null.db", NULLABLE_DDL)
        _register_nullable_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET assignee='dave' WHERE id=2")

            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(text("UPDATE tasks SET assignee=NULL WHERE id=2"))

            with pytest.raises(ConflictError) as excinfo:
                sandbox.commit_to_production()
            assert excinfo.value.columns == ["assignee"]

        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT assignee FROM tasks WHERE id=2")).scalar_one()
                is None
            )

    def test_the_agent_can_set_a_column_to_null(self, tmp_path):
        engine, _ = _prod(tmp_path, "cas_set_null.db", NULLABLE_DDL)
        _register_nullable_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET assignee=NULL WHERE id=2")
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT assignee FROM tasks WHERE id=2")).scalar_one()
                is None
            )

    def test_a_float_column_is_not_guarded(self, tmp_path):
        """Float equality does not survive round-trips reliably, so guarding on
        it would reject correct changesets. It is excluded by design."""
        from safeagentdb.sync import _is_guardable

        table = _guard_table()
        assert _is_guardable(table.c.id) is True
        assert _is_guardable(table.c.title) is True
        assert _is_guardable(table.c.score) is False

        engine, url = _prod(tmp_path, "cas_float.db", NULLABLE_DDL)
        _register_nullable_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("DELETE FROM tasks WHERE id=2")

            # A concurrent change to the unguarded float does not block it.
            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(text("UPDATE tasks SET score=99.9 WHERE id=2"))

            assert sandbox.commit_to_production() == 1

    def test_statements_run_in_a_deterministic_order(self):
        """Two changesets touching the same rows take them in the same order."""
        from safeagentdb.diff import DiffType, RowDiff
        from safeagentdb.sync import _statement_order

        diffs = [
            RowDiff(table="tasks", diff_type=DiffType.UPDATE, pk={"id": 3}),
            RowDiff(table="users", diff_type=DiffType.UPDATE, pk={"id": 1}),
            RowDiff(table="tasks", diff_type=DiffType.UPDATE, pk={"id": 1}),
            RowDiff(table="tasks", diff_type=DiffType.DELETE, pk={"id": 2}),
        ]
        ordered = [(d.table, d.pk["id"]) for d in sorted(diffs, key=_statement_order)]
        assert ordered == [("tasks", 1), ("tasks", 2), ("tasks", 3), ("users", 1)]

        # The reverse input gives the same order.
        reversed_order = [
            (d.table, d.pk["id"])
            for d in sorted(list(reversed(diffs)), key=_statement_order)
        ]
        assert reversed_order == ordered

    def test_a_row_key_matching_several_production_rows_conflicts(self, tmp_path):
        engine, url = _prod(
            tmp_path,
            "cas_multi.db",
            [
                "CREATE TABLE events (\n"
                " user_id INTEGER NOT NULL,\n"
                " kind TEXT NOT NULL,\n"
                " payload TEXT NOT NULL\n"
                ")",
                "INSERT INTO events VALUES (42, 'login', 'a')",
            ],
        )

        class EventValidator(SafeModel):
            __table_name__ = "events"
            user_id: int
            kind: str
            payload: str

        with ShadowDB(
            engine,
            tables=["events"],
            tenant_id=42,
            row_key={"events": ["user_id", "kind"]},
        ) as sandbox:
            sandbox.execute("UPDATE events SET payload='edited' WHERE kind='login'")

            # A duplicate with the same key appears in production.
            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(text("INSERT INTO events VALUES (42, 'login', 'a')"))

            with pytest.raises(ConflictError, match="does not identify a row"):
                sandbox.commit_to_production()

        with engine.connect() as conn:
            payloads = [
                r[0] for r in conn.execute(text("SELECT payload FROM events")).fetchall()
            ]
        assert payloads == ["a", "a"]
