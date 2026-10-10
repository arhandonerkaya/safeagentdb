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

            # The foreign-key entry is what reference_tables is for, so it must
            # be gone. The SERIAL primary key still reports its dropped sequence
            # default, and should: PostgreSQL generates that value and SQLite
            # cannot, which is the whole reason production assigns the key.
            # Asserting an empty list here is wrong -- it passes on SQLite,
            # where there is no sequence to lose, and fails on any real server.
            reported = sandbox.unsupported_constraints
            assert not any("FOREIGN KEY" in item for item in reported)
            assert all("nextval" in item for item in reported), reported
            assert sandbox.provisional_key_columns == {"items": ["id"]}

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


# ============================================================
# Review findings that need a real server: 2, 3 and 6
# ============================================================


class TestCascadeOnServer:
    """Finding 2. SQLite reports referential actions only through a pragma;
    PostgreSQL reports them through the inspector, so detection takes a
    different code path here."""

    CASCADE = [
        """
        CREATE TABLE cparent (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            label TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE cchild (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            parent_id INTEGER NOT NULL REFERENCES cparent(id) ON DELETE CASCADE,
            note TEXT NOT NULL
        )
        """,
        "INSERT INTO cparent (id, user_id, label) VALUES (1, 42, 'owned by 42')",
        "INSERT INTO cchild (user_id, parent_id, note) VALUES (99, 1, 'belongs to 99')",
    ]

    @staticmethod
    def _register():
        class CParentValidator(SafeModel):
            __table_name__ = "cparent"
            id: int
            user_id: int
            label: str

        return CParentValidator

    def test_the_inspector_path_finds_the_cascade(self, make_tables):
        engine = make_tables(self.CASCADE, ["cchild", "cparent"])
        self._register()

        with ShadowDB(engine, tables=["cparent"], tenant_id=42) as sandbox:
            reported = sandbox.unsupported_constraints
            assert any("cchild" in item for item in reported), reported
            assert any("CASCADE" in item.upper() for item in reported), reported
            assert [r.child_table for r in sandbox.cascade_references] == ["cchild"]

    def test_a_cross_tenant_cascade_is_refused_on_the_server(self, make_tables):
        from safeagentdb import CascadeError

        engine = make_tables(self.CASCADE, ["cchild", "cparent"])
        self._register()

        with ShadowDB(engine, tables=["cparent"], tenant_id=42) as sandbox:
            sandbox.execute("DELETE FROM cparent WHERE id=1")
            with pytest.raises(CascadeError) as excinfo:
                sandbox.commit_to_production()
            assert excinfo.value.referencing_table == "cchild"
            assert 99 in excinfo.value.tenants

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM cparent")).scalar_one() == 1
            assert conn.execute(text("SELECT COUNT(*) FROM cchild")).scalar_one() == 1


class TestJsonOnServer:
    """Finding 3. On PostgreSQL the driver really does hand back a dict and a
    list, which is what crashed the sandbox open."""

    DOCS = [
        """
        CREATE TABLE docs (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            data JSONB,
            tags TEXT[]
        )
        """,
        "INSERT INTO docs (user_id, title, data, tags) VALUES "
        "(42, 'first', '{\"a\": 1}', ARRAY['x','y'])",
        "INSERT INTO docs (user_id, title, data, tags) VALUES "
        "(42, 'second', '{\"outer\": {\"inner\": [1, 2]}}', NULL)",
        "INSERT INTO docs (user_id, title, data, tags) VALUES "
        "(42, 'third', NULL, NULL)",
    ]

    @staticmethod
    def _register():
        class DocValidator(SafeModel):
            __table_name__ = "docs"
            id: int
            user_id: int
            title: str
            data: dict | list | None
            tags: list | None

        return DocValidator

    def test_the_sandbox_opens_with_jsonb_and_array_columns(self, make_tables):
        engine = make_tables(self.DOCS, ["docs"])
        self._register()

        with ShadowDB(engine, tables=["docs"], tenant_id=42) as sandbox:
            assert sandbox.clone_stats["docs"] == 3
            titles = sorted(r["title"] for r in sandbox.query("SELECT title FROM docs"))
            assert titles == ["first", "second", "third"]
            # SERIAL, so production assigns new keys.
            assert sandbox.generated_columns == {"docs": ["id"]}

    def test_an_untouched_json_value_is_not_reported_as_changed(self, make_tables):
        engine = make_tables(self.DOCS, ["docs"])
        self._register()

        with ShadowDB(engine, tables=["docs"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE docs SET title='renamed' WHERE title='first'")
            changeset = sandbox.diff()
            (diff,) = changeset.diffs
            assert diff.changed_columns() == ["title"]
            assert changeset.is_valid is True
            assert sandbox.commit_to_production(changeset=changeset) == 1

        with engine.connect() as conn:
            data, tags = conn.execute(
                text("SELECT data, tags FROM docs WHERE title='renamed'")
            ).one()
        assert data == {"a": 1}
        assert list(tags) == ["x", "y"]

    def test_a_changed_json_value_round_trips_to_the_server(self, make_tables):
        engine = make_tables(self.DOCS, ["docs"])
        self._register()

        with ShadowDB(engine, tables=["docs"], tenant_id=42) as sandbox:
            sandbox.execute(
                "UPDATE docs SET data='{\"a\": 2, \"b\": [1, 2]}' WHERE title='first'"
            )
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            data = conn.execute(
                text("SELECT data FROM docs WHERE title='first'")
            ).scalar_one()
        assert data == {"a": 2, "b": [1, 2]}


class TestStatementOrderOnServer:
    """Finding 6. PostgreSQL checks foreign keys immediately, so a child
    inserted before its parent is rejected outright."""

    ORDER = [
        """
        CREATE TABLE zparent (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            label TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE achild (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            parent_id INTEGER NOT NULL REFERENCES zparent(id),
            note TEXT NOT NULL
        )
        """,
    ]

    @staticmethod
    def _register():
        class ZParentValidator(SafeModel):
            __table_name__ = "zparent"
            id: int
            user_id: int
            label: str

        class AChildValidator(SafeModel):
            __table_name__ = "achild"
            id: int
            user_id: int
            parent_id: int
            note: str

        return ZParentValidator, AChildValidator

    def test_a_parent_is_inserted_before_its_child(self, make_tables):
        """achild sorts before zparent, so this is exactly the case the old
        alphabetical ordering got wrong."""
        engine = make_tables(self.ORDER, ["achild", "zparent"])
        self._register()

        with ShadowDB(engine, tables=["achild", "zparent"], tenant_id=42) as sandbox:
            sandbox.execute(
                "INSERT INTO zparent (id, user_id, label) VALUES (1, 42, 'parent')"
            )
            sandbox.execute(
                "INSERT INTO achild (id, user_id, parent_id, note) "
                "VALUES (10, 42, 1, 'child')"
            )
            assert sandbox.commit_to_production() == 2

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM zparent")).scalar_one() == 1
            assert conn.execute(text("SELECT COUNT(*) FROM achild")).scalar_one() == 1

    def test_a_child_is_deleted_before_its_parent(self, make_tables):
        engine = make_tables(
            self.ORDER
            + [
                "INSERT INTO zparent VALUES (1, 42, 'parent')",
                "INSERT INTO achild VALUES (10, 42, 1, 'child')",
            ],
            ["achild", "zparent"],
        )
        self._register()

        with ShadowDB(engine, tables=["achild", "zparent"], tenant_id=42) as sandbox:
            sandbox.execute("DELETE FROM achild WHERE id=10")
            sandbox.execute("DELETE FROM zparent WHERE id=1")
            assert sandbox.commit_to_production() == 2

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM zparent")).scalar_one() == 0
            assert conn.execute(text("SELECT COUNT(*) FROM achild")).scalar_one() == 0
