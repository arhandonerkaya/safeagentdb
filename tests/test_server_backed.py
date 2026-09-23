"""
test_server_backed.py -- The claims that SQLite alone cannot verify.

Everything else in the suite uses SQLite as the stand-in production database.
That is enough to exercise the logic, but it cannot show that the guarantees
hold on a real server with real concurrency, real sequences and real foreign
keys. These tests do, and CI runs them against a PostgreSQL service container.

They are skipped unless DATABASE_URL is set, so nothing changes locally:

    DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost/safeagentdb \\
        python -m pytest -m requires_db
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from safeagentdb import (
    ConflictError,
    GeneratedValueError,
    IntegrityViolationError,
    SafeModel,
    ShadowDB,
)
from safeagentdb.engine import PROVISIONAL_KEY_BASE
from safeagentdb.models import _model_registry

pytestmark = pytest.mark.requires_db


@pytest.fixture(autouse=True)
def _isolated_registry():
    saved = dict(_model_registry)
    _model_registry.clear()
    yield
    _model_registry.clear()
    _model_registry.update(saved)


TASKS = [
    """
    CREATE TABLE tasks (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'todo',
        assignee TEXT
    )
    """,
    "INSERT INTO tasks (user_id, title, status) VALUES (42, 'mine a', 'todo')",
    "INSERT INTO tasks (user_id, title, status) VALUES (99, 'theirs', 'todo')",
    "INSERT INTO tasks (user_id, title, status) VALUES (42, 'mine b', 'todo')",
]


def _register_task_validator():
    class TaskValidator(SafeModel):
        __table_name__ = "tasks"
        id: int
        user_id: int
        title: str
        status: str
        assignee: str | None

    return TaskValidator


def _tenant_ids(engine, tenant: int) -> list[int]:
    with engine.connect() as conn:
        return [
            r[0]
            for r in conn.execute(
                text("SELECT id FROM tasks WHERE user_id = :t ORDER BY id"),
                {"t": tenant},
            )
        ]


# ============================================================
# Tenant isolation
# ============================================================


class TestTenantIsolationOnServer:
    def test_only_the_tenants_rows_are_cloned_and_written(self, make_tables, database_url):
        engine = make_tables(TASKS, ["tasks"])
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            rows = sandbox.query("SELECT user_id FROM tasks")
            assert rows and all(r["user_id"] == 42 for r in rows)

            sandbox.execute("UPDATE tasks SET status = 'done'")
            assert sandbox.commit_to_production() == 2

        with engine.connect() as conn:
            statuses = dict(
                conn.execute(text("SELECT user_id, status FROM tasks ORDER BY id")).fetchall()
            )
        assert statuses[42] == "done"
        assert statuses[99] == "todo"

    def test_a_cross_tenant_row_is_refused(self, make_tables):
        engine = make_tables(TASKS, ["tasks"])
        _register_task_validator()

        from safeagentdb import SyncError

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET user_id = 99")
            with pytest.raises(SyncError, match="Tenant breach"):
                sandbox.commit_to_production()

        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT COUNT(*) FROM tasks WHERE user_id = 42")
            ).scalar_one() == 2


# ============================================================
# Compare-and-swap, against a real concurrent transaction
# ============================================================


class TestCompareAndSwapOnServer:
    def test_a_committed_concurrent_update_is_detected(self, make_tables, database_url):
        engine = make_tables(TASKS, ["tasks"])
        _register_task_validator()
        first = _tenant_ids(engine, 42)[0]

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute(f"UPDATE tasks SET status = 'done' WHERE id = {first}")

            other = create_engine(database_url)
            try:
                with other.begin() as conn:
                    conn.execute(
                        text("UPDATE tasks SET status = 'blocked' WHERE id = :i"),
                        {"i": first},
                    )
            finally:
                other.dispose()

            with pytest.raises(ConflictError) as excinfo:
                sandbox.commit_to_production()
            assert excinfo.value.columns == ["status"]

        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT status FROM tasks WHERE id = :i"), {"i": first}
            ).scalar_one() == "blocked"

    def test_an_edit_to_another_column_coexists(self, make_tables, database_url):
        engine = make_tables(TASKS, ["tasks"])
        _register_task_validator()
        first = _tenant_ids(engine, 42)[0]

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute(f"UPDATE tasks SET status = 'done' WHERE id = {first}")

            other = create_engine(database_url)
            try:
                with other.begin() as conn:
                    conn.execute(
                        text("UPDATE tasks SET title = 'renamed by human' WHERE id = :i"),
                        {"i": first},
                    )
            finally:
                other.dispose()

            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            title, status = conn.execute(
                text("SELECT title, status FROM tasks WHERE id = :i"), {"i": first}
            ).one()
        assert title == "renamed by human"
        assert status == "done"

    def test_a_null_guard_round_trips(self, make_tables, database_url):
        """assignee is NULL at clone time. 'col = NULL' would never match."""
        engine = make_tables(TASKS, ["tasks"])
        _register_task_validator()
        first = _tenant_ids(engine, 42)[0]

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute(f"UPDATE tasks SET assignee = 'bob' WHERE id = {first}")
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT assignee FROM tasks WHERE id = :i"), {"i": first}
            ).scalar_one() == "bob"

    def test_a_concurrent_delete_is_detected(self, make_tables, database_url):
        engine = make_tables(TASKS, ["tasks"])
        _register_task_validator()
        first = _tenant_ids(engine, 42)[0]

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute(f"UPDATE tasks SET status = 'done' WHERE id = {first}")

            other = create_engine(database_url)
            try:
                with other.begin() as conn:
                    conn.execute(
                        text("DELETE FROM tasks WHERE id = :i"), {"i": first}
                    )
            finally:
                other.dispose()

            with pytest.raises(ConflictError, match="deleted in production"):
                sandbox.commit_to_production()


# ============================================================
# Sequences: the key really comes from production
# ============================================================


class TestGeneratedKeysOnServer:
    def test_production_assigns_the_key(self, make_tables):
        engine = make_tables(TASKS, ["tasks"])
        _register_task_validator()

        with engine.connect() as conn:
            before = conn.execute(text("SELECT MAX(id) FROM tasks")).scalar_one()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            assert sandbox.provisional_key_columns == {"tasks": ["id"]}

            sandbox.execute(
                "INSERT INTO tasks (user_id, title, status) "
                "VALUES (42, 'from the agent', 'todo')"
            )
            changeset = sandbox.diff()
            (diff,) = changeset.diffs
            assert diff.new["id"] >= PROVISIONAL_KEY_BASE
            assert changeset.is_valid is True

            assert sandbox.commit_to_production() == 1
            (assignment,) = sandbox.assigned_keys
            assert assignment.assigned["id"] == before + 1

        with engine.connect() as conn:
            row = conn.execute(
                text("SELECT id, user_id FROM tasks WHERE title = 'from the agent'")
            ).one()
        assert row[0] == before + 1
        assert row[1] == 42

    def test_the_sequence_stays_in_step(self, make_tables):
        """The whole point: a later ordinary insert must not collide."""
        engine = make_tables(TASKS, ["tasks"])
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute(
                "INSERT INTO tasks (user_id, title, status) "
                "VALUES (42, 'agent row', 'todo')"
            )
            sandbox.commit_to_production()

        # The application inserts as it always would.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tasks (user_id, title, status) "
                    "VALUES (42, 'app row', 'todo')"
                )
            )

        with engine.connect() as conn:
            ids = [r[0] for r in conn.execute(text("SELECT id FROM tasks ORDER BY id"))]
        assert len(ids) == len(set(ids))

    def test_an_explicit_key_is_refused(self, make_tables):
        engine = make_tables(TASKS, ["tasks"])
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute(
                "INSERT INTO tasks (id, user_id, title, status) "
                "VALUES (9999, 42, 'explicit', 'todo')"
            )
            assert sandbox.diff().is_valid is False
            with pytest.raises(GeneratedValueError):
                sandbox.commit_to_production()

        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT COUNT(*) FROM tasks WHERE id = 9999")
            ).scalar_one() == 0


# ============================================================
# Foreign keys against a real server
# ============================================================


LOOKUP = [
    """
    CREATE TABLE statuses (
        id INTEGER PRIMARY KEY,
        label TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE items (
        id SERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL,
        status_id INTEGER NOT NULL REFERENCES statuses(id),
        title TEXT NOT NULL
    )
    """,
    "INSERT INTO statuses VALUES (1, 'todo'), (2, 'done')",
    "INSERT INTO items (user_id, status_id, title) VALUES (42, 1, 'existing')",
]


def _register_lookup_validators():
    class StatusValidator(SafeModel):
        __table_name__ = "statuses"
        id: int
        label: str

    class ItemValidator(SafeModel):
        __table_name__ = "items"
        id: int
        user_id: int
        status_id: int
        title: str

    return StatusValidator, ItemValidator


class TestForeignKeysOnServer:
    def test_an_empty_parent_does_not_block_a_valid_reference(self, make_tables):
        engine = make_tables(LOOKUP, ["items", "statuses"])
        _register_lookup_validators()

        with ShadowDB(engine, tables=["items"], tenant_id=42) as sandbox:
            assert sandbox.clone_stats["statuses"] == 0
            assert any(
                "statuses" in item and "cloned 0 rows" in item
                for item in sandbox.unsupported_constraints
            )
            sandbox.execute(
                "INSERT INTO items (user_id, status_id, title) "
                "VALUES (42, 2, 'references a real production row')"
            )
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT COUNT(*) FROM items WHERE status_id = 2")
            ).scalar_one() == 1

    def test_reference_tables_restores_enforcement(self, make_tables):
        engine = make_tables(LOOKUP, ["items", "statuses"])
        _register_lookup_validators()

        from sqlalchemy.exc import IntegrityError

        with ShadowDB(
            engine, tables=["items"], tenant_id=42, reference_tables=["statuses"]
        ) as sandbox:
            assert sandbox.clone_stats["statuses"] == 2
            assert sandbox.unsupported_constraints == []

            with pytest.raises(IntegrityError):
                sandbox.execute(
                    "INSERT INTO items (id, user_id, status_id, title) "
                    "VALUES (500, 42, 999, 'dangling')"
                )
                sandbox.session.commit()

    def test_production_rejects_what_the_sandbox_could_not_see(self, make_tables):
        """A real foreign key violation production catches and we wrap."""
        engine = make_tables(LOOKUP, ["items", "statuses"])
        _register_lookup_validators()

        with ShadowDB(engine, tables=["items"], tenant_id=42) as sandbox:
            # statuses cloned nothing, so the sandbox cannot check this.
            sandbox.execute(
                "INSERT INTO items (user_id, status_id, title) "
                "VALUES (42, 4242, 'no such status')"
            )
            assert sandbox.diff().is_valid is True

            with pytest.raises(IntegrityViolationError) as excinfo:
                sandbox.commit_to_production()
            assert excinfo.value.table == "items"
            assert excinfo.value.__cause__ is not None

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM items")).scalar_one() == 1
