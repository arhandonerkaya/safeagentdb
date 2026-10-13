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
# Identity keys: the serial policy, and B-1's collision
# ============================================================


def _identity_tasks(kind: str) -> list[str]:
    """Tenant 42 holds ids 1-4, tenant 99 ids 5-6, as in B-1.

    Seeded without explicit ids, so GENERATED ALWAYS accepts the rows and the
    identity sequence is in step with them.
    """
    return [
        f"""
        CREATE TABLE tasks (
            id INTEGER GENERATED {kind} AS IDENTITY PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'todo',
            assignee TEXT
        )
        """,
        "INSERT INTO tasks (user_id, title) VALUES "
        "(42, 'a'), (42, 'b'), (42, 'c'), (42, 'd'), (99, 'e'), (99, 'f')",
    ]


@pytest.mark.parametrize("kind", ["BY DEFAULT", "ALWAYS"])
class TestIdentityKeysOnServer:
    def test_the_tenant_without_the_highest_ids_can_insert(self, make_tables, kind):
        engine = make_tables(_identity_tasks(kind), ["tasks"])
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            assert sandbox.provisional_key_columns == {"tasks": ["id"]}
            sandbox.execute("INSERT INTO tasks (user_id, title) VALUES (42, 'new')")
            changeset = sandbox.diff()
            (diff,) = changeset.diffs
            assert diff.new["id"] >= PROVISIONAL_KEY_BASE
            assert changeset.is_valid is True

            assert sandbox.commit_to_production() == 1
            (assignment,) = sandbox.assigned_keys
            assert assignment.provisional == {"id": diff.new["id"]}
            assert assignment.assigned == {"id": 7}

        with engine.connect() as conn:
            row = conn.execute(text("SELECT user_id, title FROM tasks WHERE id = 7")).one()
        assert tuple(row) == (42, "new")

    def test_both_tenants_insert_and_the_sequence_stays_in_step(self, make_tables, kind):
        engine = make_tables(_identity_tasks(kind), ["tasks"])
        _register_task_validator()

        for tenant in (42, 99):
            with ShadowDB(engine, tables=["tasks"], tenant_id=tenant) as sandbox:
                sandbox.execute(
                    "INSERT INTO tasks (user_id, title) VALUES (:t, 'new')", {"t": tenant}
                )
                sandbox.commit_to_production()

        # The application inserts as it always would.
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO tasks (user_id, title) VALUES (42, 'app row')"))

        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, user_id FROM tasks WHERE id > 6 ORDER BY id")
            ).fetchall()
        assert [tuple(r) for r in rows] == [(7, 42), (8, 99), (9, 42)]

    def test_an_explicit_key_is_refused(self, make_tables, kind):
        engine = make_tables(_identity_tasks(kind), ["tasks"])
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("INSERT INTO tasks (id, user_id, title) VALUES (9999, 42, 'explicit')")
            assert sandbox.diff().is_valid is False
            with pytest.raises(GeneratedValueError, match="supplies a value"):
                sandbox.commit_to_production()

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one() == 6


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

        from safeagentdb import AgentSQLError

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

            with pytest.raises(AgentSQLError) as agent_error:
                sandbox.execute(
                    "INSERT INTO items (id, user_id, status_id, title) "
                    "VALUES (500, 42, 999, 'dangling')"
                )
                sandbox.session.commit()
            assert isinstance(agent_error.value.__cause__, IntegrityError)

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

    AUDIT = [
        """
        CREATE TABLE atasks (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE aaudit (
            id SERIAL PRIMARY KEY,
            task_id INTEGER NOT NULL REFERENCES atasks(id) ON DELETE CASCADE,
            event TEXT NOT NULL
        )
        """,
        "INSERT INTO atasks (id, user_id, title) VALUES (1, 42, 'mine')",
        "INSERT INTO aaudit (task_id, event) VALUES (1, 'created')",
    ]

    def test_an_unattributable_cascade_is_refused_on_the_server(self, make_tables):
        """aaudit has no user_id column, so its rows cannot be attributed to a
        tenant. An unknown owner must not be read as the acting tenant."""
        from safeagentdb import CascadeError

        engine = make_tables(self.AUDIT, ["aaudit", "atasks"])

        class ATaskValidator(SafeModel):
            __table_name__ = "atasks"
            id: int
            user_id: int
            title: str

        with ShadowDB(engine, tables=["atasks"], tenant_id=42) as sandbox:
            assert any(
                "aaudit" in item and "never cloned" in item
                for item in sandbox.unsupported_constraints
            ), sandbox.unsupported_constraints

            sandbox.execute("DELETE FROM atasks WHERE id=1")
            with pytest.raises(CascadeError) as excinfo:
                sandbox.commit_to_production()

        assert excinfo.value.referencing_table == "aaudit"
        assert excinfo.value.unattributed_rows == 1
        assert excinfo.value.tenants == []

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM atasks")).scalar_one() == 1
            assert conn.execute(text("SELECT COUNT(*) FROM aaudit")).scalar_one() == 1


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


# ============================================================
# Pre-0.3.0 review (docs/REVIEW_0_3.md), against PostgreSQL
# ============================================================


def _validator(table: str, **fields):
    """Register a SafeModel for ``table`` with the given field annotations."""
    namespace = {"__table_name__": table, "__annotations__": fields}
    return type(f"V_{table}", (SafeModel,), namespace)


R3_PARENT_CHILD = [
    "CREATE TABLE r3_parent (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
    "CREATE TABLE r3_child (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " parent_id INTEGER NOT NULL REFERENCES r3_parent(id) {actions})",
]


def _r3_parent_child(make_tables, actions: str, rows: list[str]):
    ddl = [R3_PARENT_CHILD[0], R3_PARENT_CHILD[1].format(actions=actions), *rows]
    engine = make_tables(ddl, ["r3_child", "r3_parent"])
    _validator("r3_parent", id=int, user_id=int)
    _validator("r3_child", id=int, user_id=int, parent_id=int)
    return engine


def _scalar(engine, sql: str):
    with engine.connect() as conn:
        return conn.execute(text(sql)).scalar_one()


class TestReviewKeyChangesOnServer:
    """P1-1: suspected in the review to occur on PostgreSQL as on SQLite."""

    def test_a_key_change_is_refused(self, make_tables):
        from safeagentdb import SyncError

        engine = _r3_parent_child(
            make_tables,
            "ON UPDATE CASCADE ON DELETE CASCADE",
            [
                "INSERT INTO r3_parent VALUES (1, 42)",
                "INSERT INTO r3_child VALUES (10, 42, 1), (11, 42, 1)",
            ],
        )
        with ShadowDB(engine, tables=["r3_parent"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE r3_parent SET id = 5 WHERE id = 1")
            assert sandbox.diff().is_valid is False
            with pytest.raises(SyncError, match="row key"):
                sandbox.commit_to_production()

        assert _scalar(engine, "SELECT COUNT(*) FROM r3_child WHERE parent_id = 1") == 2

    def test_a_same_tenant_cascade_needs_allow_cascades(self, make_tables):
        from safeagentdb import CascadeError

        rows = [
            "INSERT INTO r3_parent VALUES (1, 42)",
            "INSERT INTO r3_child VALUES (10, 42, 1)",
        ]
        engine = _r3_parent_child(make_tables, "ON DELETE CASCADE", rows)

        with ShadowDB(engine, tables=["r3_parent"], tenant_id=42) as sandbox:
            sandbox.execute("DELETE FROM r3_parent WHERE id = 1")
            with pytest.raises(CascadeError, match="allow_cascades"):
                sandbox.commit_to_production()
        assert _scalar(engine, "SELECT COUNT(*) FROM r3_child") == 1

        with ShadowDB(
            engine, tables=["r3_parent"], tenant_id=42, allow_cascades=True
        ) as sandbox:
            sandbox.execute("DELETE FROM r3_parent WHERE id = 1")
            assert sandbox.commit_to_production() == 1
        assert _scalar(engine, "SELECT COUNT(*) FROM r3_child") == 0


class TestReviewStatementOrderOnServer:
    """P1-2 and B2. PostgreSQL checks a NOT DEFERRABLE foreign key per
    statement, so the order is what decides."""

    def test_reparent_then_delete(self, make_tables):
        engine = _r3_parent_child(
            make_tables,
            "ON DELETE CASCADE",
            [
                "INSERT INTO r3_parent VALUES (1, 42), (2, 42)",
                "INSERT INTO r3_child VALUES (10, 42, 1)",
            ],
        )
        with ShadowDB(engine, tables=["r3_parent", "r3_child"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE r3_child SET parent_id = 2 WHERE id = 10")
            sandbox.execute("DELETE FROM r3_parent WHERE id = 1")
            assert sandbox.commit_to_production() == 2

        assert _scalar(engine, "SELECT parent_id FROM r3_child WHERE id = 10") == 2

    EMP = [
        "CREATE TABLE r3_emp (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
        " manager_id INTEGER REFERENCES r3_emp(id))",
        "INSERT INTO r3_emp VALUES (1, 42, NULL), (2, 42, 1)",
    ]

    def test_a_self_referencing_table(self, make_tables):
        engine = make_tables(self.EMP, ["r3_emp"])
        _validator("r3_emp", id=int, user_id=int, manager_id=int | None)

        with ShadowDB(engine, tables=["r3_emp"], tenant_id=42) as sandbox:
            sandbox.execute("INSERT INTO r3_emp VALUES (20, 42, NULL)")
            sandbox.execute("INSERT INTO r3_emp VALUES (10, 42, 20)")
            sandbox.execute("DELETE FROM r3_emp WHERE id = 2")
            sandbox.execute("DELETE FROM r3_emp WHERE id = 1")
            assert sandbox.commit_to_production() == 4

        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, manager_id FROM r3_emp ORDER BY id")
            ).fetchall()
        assert [tuple(r) for r in rows] == [(10, 20), (20, None)]


class TestReviewTypesOnServer:
    """P2-2, P2-3 (timestamptz, suspected in the review), P1-5 and B1."""

    EVENTS = [
        "CREATE TABLE r3_ev (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
        " note TEXT NOT NULL, at TIMESTAMPTZ, code JSONB, external_id BIGINT)",
        "INSERT INTO r3_ev VALUES (1, 42, 'n', '2024-01-01 10:00:00+02:00',"
        " '\"123\"', 1234567890123456789)",
        "INSERT INTO r3_ev VALUES (2, 42, 'n', '2024-01-01 10:00:00+00:00',"
        " 'true', NULL)",
    ]

    def _engine(self, make_tables):
        import datetime as dt
        from typing import Any

        engine = make_tables(self.EVENTS, ["r3_ev"])
        _validator(
            "r3_ev",
            id=int,
            user_id=int,
            note=str,
            at=dt.datetime | None,
            code=Any,
            external_id=int | None,
        )
        return engine

    def test_a_copied_row_keeps_its_instant_and_json_types(self, make_tables):
        engine = self._engine(make_tables)
        with ShadowDB(engine, tables=["r3_ev"], tenant_id=42) as sandbox:
            sandbox.execute(
                "INSERT INTO r3_ev"
                " SELECT id + 10, user_id, note, at, code, external_id FROM r3_ev"
            )
            changeset = sandbox.diff()
            assert changeset.is_valid is True, changeset.validate_all()
            assert sandbox.commit_to_production(changeset=changeset) == 2

        matching = _scalar(
            engine,
            "SELECT COUNT(*) FROM r3_ev a JOIN r3_ev b ON b.id = a.id + 10"
            " WHERE a.at = b.at AND a.code = b.code"
            " AND jsonb_typeof(a.code) = jsonb_typeof(b.code)",
        )
        assert matching == 2
        assert _scalar(engine, "SELECT jsonb_typeof(code) FROM r3_ev WHERE id = 11") == "string"
        assert _scalar(engine, "SELECT jsonb_typeof(code) FROM r3_ev WHERE id = 12") == "boolean"

    def test_an_agent_written_offset_is_the_instant_stored(self, make_tables):
        engine = self._engine(make_tables)
        with ShadowDB(engine, tables=["r3_ev"], tenant_id=42) as sandbox:
            sandbox.execute(
                "UPDATE r3_ev SET at = '2025-06-01 12:00:00-05:00' WHERE id = 1"
            )
            assert sandbox.commit_to_production() == 1

        stored = _scalar(
            engine,
            "SELECT COUNT(*) FROM r3_ev WHERE id = 1"
            " AND at = TIMESTAMPTZ '2025-06-01 17:00:00+00:00'",
        )
        assert stored == 1

    def test_a_concurrent_change_to_a_timestamp_is_still_detected(
        self, make_tables, database_url
    ):
        """On PostgreSQL a datetime stays in the compare-and-swap guard."""
        engine = self._engine(make_tables)
        with ShadowDB(engine, tables=["r3_ev"], tenant_id=42) as sandbox:
            sandbox.execute(
                "UPDATE r3_ev SET at = '2030-01-01 00:00:00+00:00' WHERE id = 1"
            )
            other = create_engine(database_url)
            try:
                with other.begin() as conn:
                    conn.execute(
                        text("UPDATE r3_ev SET at = at + INTERVAL '1 hour' WHERE id = 1")
                    )
            finally:
                other.dispose()
            with pytest.raises(ConflictError) as excinfo:
                sandbox.commit_to_production()
        assert excinfo.value.columns == ["at"]

    def test_json_one_to_true_is_written(self, make_tables):
        engine = make_tables(
            [
                "CREATE TABLE r3_docs (id INTEGER PRIMARY KEY,"
                " user_id INTEGER NOT NULL, data JSONB)",
                "INSERT INTO r3_docs VALUES (1, 42, '{\"enabled\": 1}')",
            ],
            ["r3_docs"],
        )
        _validator("r3_docs", id=int, user_id=int, data=dict | None)
        with ShadowDB(engine, tables=["r3_docs"], tenant_id=42) as sandbox:
            sandbox.execute(
                "UPDATE r3_docs SET data = '{\"enabled\": true}' WHERE id = 1"
            )
            assert sandbox.commit_to_production() == 1

        kind = _scalar(engine, "SELECT jsonb_typeof(data->'enabled') FROM r3_docs WHERE id = 1")
        assert kind == "boolean"

    def test_a_large_integer_and_a_string_tenant_id(self, make_tables):
        engine = self._engine(make_tables)
        with ShadowDB(engine, tables=["r3_ev"], tenant_id="42") as sandbox:
            assert sandbox.tenant_id == 42
            sandbox.execute("UPDATE r3_ev SET note = 'edited' WHERE id = 1")
            sandbox.execute("DELETE FROM r3_ev WHERE id = 2")
            changeset = sandbox.diff()
            assert changeset.is_valid is True, (
                changeset.blocking_errors,
                changeset.validate_all(),
            )
            assert sandbox.commit_to_production(changeset=changeset) == 2

        external = _scalar(engine, "SELECT external_id FROM r3_ev WHERE id = 1")
        assert external == 1234567890123456789
        assert _scalar(engine, "SELECT COUNT(*) FROM r3_ev") == 1


class TestRound4OnServer:
    """Round 4 of the review: the cases only a server can show."""

    NAIVE = [
        "CREATE TABLE r4_ev (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
        " at TIMESTAMP WITHOUT TIME ZONE)",
        "INSERT INTO r4_ev VALUES (1, 42, '2024-01-01 10:00:00')",
    ]

    def _naive(self, make_tables):
        import datetime as dt

        engine = make_tables(self.NAIVE, ["r4_ev"])
        _validator("r4_ev", id=int, user_id=int, at=dt.datetime | None)
        return engine

    @pytest.mark.parametrize(
        "value", ["2024-06-01 12:00:00+02:00", "2024-06-01T12:00:00Z"]
    )
    def test_an_offset_into_a_naive_timestamp_is_refused(self, make_tables, value):
        """Suspected in round 4: PostgreSQL converted by the session TimeZone
        and dropped the offset without saying so."""
        from safeagentdb import SyncError

        engine = self._naive(make_tables)
        with ShadowDB(engine, tables=["r4_ev"], tenant_id=42) as sandbox:
            sandbox.execute(f"UPDATE r4_ev SET at = '{value}' WHERE id = 1")
            sandbox.execute(f"INSERT INTO r4_ev VALUES (2, 42, '{value}')")
            changeset = sandbox.diff()
            assert changeset.is_valid is False
            assert all("time zone" in msg for _, ok, msg in changeset.validate_all() if not ok)
            with pytest.raises(SyncError, match="time zone"):
                sandbox.commit_to_production()

        assert _scalar(engine, "SELECT COUNT(*) FROM r4_ev") == 1
        assert str(_scalar(engine, "SELECT at FROM r4_ev WHERE id = 1")) == "2024-01-01 10:00:00"

    def test_a_naive_value_is_still_written(self, make_tables):
        engine = self._naive(make_tables)
        with ShadowDB(engine, tables=["r4_ev"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE r4_ev SET at = '2024-06-01 12:00:00' WHERE id = 1")
            sandbox.execute("INSERT INTO r4_ev SELECT 2, user_id, at FROM r4_ev WHERE id = 1")
            changeset = sandbox.diff()
            assert changeset.is_valid is True, changeset.validate_all()
            assert sandbox.commit_to_production(changeset=changeset) == 2

        assert _scalar(engine, "SELECT COUNT(*) FROM r4_ev WHERE at = '2024-06-01 12:00:00'") == 2

    def test_a_numeric_tenant_on_an_uncloned_child_is_the_tenant(self, make_tables):
        """Suspected in round 4: psycopg2 returns Decimal('42') for a numeric
        column, which same_tenant() could not compare with 42."""
        engine = make_tables(
            [
                "CREATE TABLE r4_parent (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
                "CREATE TABLE r4_share (id INTEGER PRIMARY KEY, user_id NUMERIC NOT NULL,"
                " parent_id INTEGER REFERENCES r4_parent(id) ON DELETE CASCADE)",
                "INSERT INTO r4_parent VALUES (1, 42)",
                "INSERT INTO r4_share VALUES (100, 42, 1)",
            ],
            ["r4_share", "r4_parent"],
        )
        _validator("r4_parent", id=int, user_id=int)
        with ShadowDB(
            engine, tables=["r4_parent"], tenant_id=42, allow_cascades=True
        ) as sandbox:
            sandbox.execute("DELETE FROM r4_parent WHERE id = 1")
            assert sandbox.commit_to_production() == 1
        assert _scalar(engine, "SELECT COUNT(*) FROM r4_share") == 0

    def test_a_numeric_tenant_column_takes_an_integer_tenant_id(self, make_tables):
        import decimal

        engine = make_tables(
            [
                "CREATE TABLE r4_num (id INTEGER PRIMARY KEY, user_id NUMERIC(10,0) NOT NULL,"
                " title TEXT)",
                "INSERT INTO r4_num VALUES (1, 42, 'a'), (2, 7, 'theirs')",
            ],
            ["r4_num"],
        )
        _validator("r4_num", id=int, user_id=decimal.Decimal, title=str)
        with ShadowDB(engine, tables=["r4_num"], tenant_id=42) as sandbox:
            assert sandbox.query("SELECT id FROM r4_num") == [{"id": 1}]
            sandbox.execute("UPDATE r4_num SET title = 'b' WHERE id = 1")
            assert sandbox.commit_to_production() == 1
        assert _scalar(engine, "SELECT title FROM r4_num WHERE id = 2") == "theirs"

    def test_updates_are_ordered_by_references_to_a_unique_column(self, make_tables):
        """R4-3 on a server that checks every NOT DEFERRABLE foreign key per
        statement."""
        engine = make_tables(
            [
                "CREATE TABLE r4_p (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " code TEXT NOT NULL UNIQUE)",
                "CREATE TABLE r4_c (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " pcode TEXT REFERENCES r4_p(code))",
                "INSERT INTO r4_p VALUES (1, 42, 'A'), (2, 42, 'C')",
                "INSERT INTO r4_c VALUES (10, 42, 'A')",
            ],
            ["r4_c", "r4_p"],
        )
        _validator("r4_p", id=int, user_id=int, code=str)
        _validator("r4_c", id=int, user_id=int, pcode=str | None)
        with ShadowDB(engine, tables=["r4_p", "r4_c"], tenant_id=42) as sandbox:
            # The child leaves 'A' before the parent gives it up, and the
            # parent takes 'B' before the new child references it.
            sandbox.execute("UPDATE r4_c SET pcode = 'C' WHERE id = 10")
            sandbox.execute("UPDATE r4_p SET code = 'B' WHERE id = 1")
            sandbox.execute("INSERT INTO r4_c VALUES (11, 42, 'B')")
            assert sandbox.commit_to_production() == 3

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, pcode FROM r4_c ORDER BY id")).fetchall()
        assert [tuple(r) for r in rows] == [(10, "C"), (11, "B")]
