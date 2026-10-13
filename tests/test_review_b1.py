"""
test_review_b1.py -- Finding B-1 of docs/REVIEW_0_3.md, found by the benchmark.

A key the sandbox assigned collided with a row the clone never saw: on a SQLite
production whose key is a rowid alias (``id INTEGER PRIMARY KEY``), the sandbox
counted on from the highest *cloned* id, which belongs to one tenant, so every
tenant but the one holding the highest ids had its INSERT rejected at commit.
Reproduction: docs/review-repros/b/b1_rowid_key_collision.py. Written before
the fix.

The policy for a rowid alias:

* an INSERT that omits the key is held provisionally, exactly like a key whose
  production default the sandbox lost: a placeholder that cannot collide, left
  out at commit, assigned by production and reported in ``assigned_keys``;
* an INSERT that supplies the key writes it as given. Applications assign these
  ids themselves, so production's uniqueness check decides.

MySQL ``AUTO_INCREMENT`` behaves the same way and gets the same policy. Those
tests need a server and run only when ``MYSQL_DATABASE_URL`` is set, e.g.

    MYSQL_DATABASE_URL=mysql+pymysql://root:root@localhost:33306/safeagentdb \\
        python -m pytest tests/test_review_b1.py
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from sqlalchemy import create_engine, event, text

from safeagentdb import (
    GeneratedValueError,
    IntegrityViolationError,
    SafeModel,
    ShadowDB,
)
from safeagentdb.engine import PROVISIONAL_KEY_BASE
from safeagentdb.models import _model_registry


@pytest.fixture(autouse=True)
def _isolated_registry():
    saved = dict(_model_registry)
    _model_registry.clear()
    yield
    _model_registry.clear()
    _model_registry.update(saved)


def _prod(tmp_path, ddl: list[str]):
    """A SQLite file database standing in for production, FKs on per connection."""
    engine = create_engine(f"sqlite:///{tmp_path / 'prod.db'}")

    @event.listens_for(engine, "connect")
    def _connect(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as conn:
        for statement in ddl:
            conn.exec_driver_sql(statement)
    return engine


def _rows(engine, sql: str) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.exec_driver_sql(sql)]


def _validator(table: str, **fields: Any) -> type[SafeModel]:
    namespace = {"__table_name__": table, "__annotations__": dict(fields)}
    return type(f"V_{table}", (SafeModel,), namespace)


# Tenant 42 holds the low ids, tenant 99 the high ones -- the benchmark's shape.
SEED = (
    "INSERT INTO tasks (id, tenant_id, title) VALUES "
    "(1, 42, 'a'), (2, 42, 'b'), (3, 42, 'c'), (4, 42, 'd'), (5, 99, 'e'), (6, 99, 'f')"
)
ALIAS = "CREATE TABLE tasks (id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, title TEXT)"


def _tasks_validator():
    return _validator("tasks", id=int, tenant_id=int, title=str)


def _open(engine, tenant: int) -> ShadowDB:
    return ShadowDB(engine, tables=["tasks"], tenant_id=tenant, tenant_column="tenant_id")


# ============================================================
# B-1  An omitted rowid-alias key is held provisionally
# ============================================================


class TestB1_OmittedKeyIsProvisional:
    def test_the_tenant_without_the_highest_ids_can_insert(self, tmp_path):
        """The repro: tenant 42's INSERT was assigned id 5, tenant 99's row."""
        engine = _prod(tmp_path, [ALIAS, SEED])
        _tasks_validator()

        with _open(engine, 42) as sandbox:
            assert sandbox.provisional_key_columns == {"tasks": ["id"]}
            sandbox.execute("INSERT INTO tasks (tenant_id, title) VALUES (42, 'new')")
            changeset = sandbox.diff()
            assert changeset.is_valid
            (insert,) = changeset.diffs
            assert insert.pk["id"] >= PROVISIONAL_KEY_BASE
            sandbox.commit_to_production(changeset)

            (assignment,) = sandbox.assigned_keys
            assert assignment.table == "tasks"
            assert assignment.provisional == {"id": insert.pk["id"]}
            assert assignment.assigned == {"id": 7}

        assert _rows(engine, "SELECT id, tenant_id, title FROM tasks WHERE id = 7") == [
            (7, 42, "new")
        ]

    def test_both_tenants_insert_in_turn(self, tmp_path):
        engine = _prod(tmp_path, [ALIAS, SEED])
        _tasks_validator()

        for tenant in (42, 99):
            with _open(engine, tenant) as sandbox:
                sandbox.execute(
                    "INSERT INTO tasks (tenant_id, title) VALUES (:t, 'new')", {"t": tenant}
                )
                sandbox.commit_to_production(sandbox.diff())

        assert _rows(engine, "SELECT id, tenant_id FROM tasks WHERE id > 6 ORDER BY id") == [
            (7, 42),
            (8, 99),
        ]

    @pytest.mark.parametrize(
        "ddl",
        [
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "tenant_id INTEGER NOT NULL, title TEXT)",
            "CREATE TABLE tasks (id INTEGER, tenant_id INTEGER NOT NULL, title TEXT, "
            "PRIMARY KEY (id))",
            "CREATE TABLE tasks (id integer, tenant_id INTEGER NOT NULL, title TEXT, "
            "PRIMARY KEY (id DESC))",
        ],
        ids=["autoincrement", "table-constraint", "table-constraint-desc"],
    )
    def test_every_spelling_of_a_rowid_alias(self, tmp_path, ddl):
        engine = _prod(tmp_path, [ddl, SEED])
        _tasks_validator()

        with _open(engine, 42) as sandbox:
            assert sandbox.provisional_key_columns == {"tasks": ["id"]}
            sandbox.execute("INSERT INTO tasks (tenant_id, title) VALUES (42, 'new')")
            sandbox.commit_to_production(sandbox.diff())
            (assignment,) = sandbox.assigned_keys
            assert assignment.assigned == {"id": 7}

    def test_several_new_rows_each_get_a_real_key(self, tmp_path):
        engine = _prod(tmp_path, [ALIAS, SEED])
        _tasks_validator()

        with _open(engine, 42) as sandbox:
            for title in ("x", "y", "z"):
                sandbox.execute(
                    "INSERT INTO tasks (tenant_id, title) VALUES (42, :t)", {"t": title}
                )
            sandbox.commit_to_production(sandbox.diff())
            assert sorted(a.assigned["id"] for a in sandbox.assigned_keys) == [7, 8, 9]

    def test_a_child_cannot_reference_the_new_row_before_it_has_a_key(self, tmp_path):
        """Same rule as a serial key: the placeholder never reaches production."""
        engine = _prod(
            tmp_path,
            [
                ALIAS,
                "CREATE TABLE notes (id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, "
                "task_id INTEGER NOT NULL REFERENCES tasks(id))",
                SEED,
            ],
        )
        _tasks_validator()
        _validator("notes", id=int, tenant_id=int, task_id=int)

        with ShadowDB(
            engine, tables=["tasks", "notes"], tenant_id=42, tenant_column="tenant_id"
        ) as sandbox:
            sandbox.execute("INSERT INTO tasks (tenant_id, title) VALUES (42, 'parent')")
            sandbox.execute(
                "INSERT INTO notes (tenant_id, task_id) "
                "SELECT 42, id FROM tasks WHERE title = 'parent'"
            )
            changeset = sandbox.diff()
            assert not changeset.is_valid
            with pytest.raises(GeneratedValueError, match="has not assigned yet"):
                sandbox.commit_to_production(changeset)

        assert _rows(engine, "SELECT count(*) FROM tasks") == [(6,)]


# ============================================================
# B-1  A key the agent supplies is written as given
# ============================================================


class TestB1_ExplicitKeyIsWrittenAsGiven:
    def test_a_free_explicit_key(self, tmp_path):
        engine = _prod(tmp_path, [ALIAS, SEED])
        _tasks_validator()

        with _open(engine, 42) as sandbox:
            sandbox.execute("INSERT INTO tasks (id, tenant_id, title) VALUES (100, 42, 'mine')")
            changeset = sandbox.diff()
            assert changeset.is_valid
            sandbox.commit_to_production(changeset)
            assert sandbox.assigned_keys == []

        assert _rows(engine, "SELECT id, tenant_id FROM tasks WHERE id = 100") == [(100, 42)]

    def test_an_explicit_key_another_tenant_holds_is_rejected_by_production(self, tmp_path):
        engine = _prod(tmp_path, [ALIAS, SEED])
        _tasks_validator()

        with _open(engine, 42) as sandbox:
            sandbox.execute("INSERT INTO tasks (id, tenant_id, title) VALUES (5, 42, 'taken')")
            changeset = sandbox.diff()
            assert changeset.is_valid  # the clone cannot see tenant 99's row 5
            with pytest.raises(IntegrityViolationError):
                sandbox.commit_to_production(changeset)

        assert _rows(engine, "SELECT tenant_id, title FROM tasks WHERE id = 5") == [(99, "e")]

    def test_an_explicit_key_at_the_sentinel_is_still_the_agents(self, tmp_path):
        """The explicit-key trigger tells it apart from a placeholder by origin."""
        engine = _prod(tmp_path, [ALIAS, SEED])
        _tasks_validator()
        chosen = PROVISIONAL_KEY_BASE + 10

        with _open(engine, 42) as sandbox:
            sandbox.execute(
                "INSERT INTO tasks (id, tenant_id, title) VALUES (:k, 42, 'big')", {"k": chosen}
            )
            sandbox.execute("INSERT INTO tasks (tenant_id, title) VALUES (42, 'generated')")
            sandbox.commit_to_production(sandbox.diff())
            (assignment,) = sandbox.assigned_keys
            assert assignment.provisional["id"] > chosen

        assert _rows(engine, f"SELECT title FROM tasks WHERE id = {chosen}") == [("big",)]
        assert _rows(
            engine, f"SELECT tenant_id FROM tasks WHERE id = {chosen + 1}"
        ) == [(42,)]

    def test_explicit_and_omitted_keys_in_one_changeset(self, tmp_path):
        engine = _prod(tmp_path, [ALIAS, SEED])
        _tasks_validator()

        with _open(engine, 42) as sandbox:
            sandbox.execute("INSERT INTO tasks (id, tenant_id, title) VALUES (50, 42, 'given')")
            sandbox.execute("INSERT INTO tasks (tenant_id, title) VALUES (42, 'assigned')")
            sandbox.commit_to_production(sandbox.diff())
            (assignment,) = sandbox.assigned_keys
            assert assignment.assigned["id"] not in range(1, 7)

        assert _rows(engine, "SELECT id FROM tasks WHERE title = 'given'") == [(50,)]


# ============================================================
# B-1  Keys that are not rowid aliases keep today's behaviour
# ============================================================


class TestB1_NotARowidAlias:
    @pytest.mark.parametrize(
        "ddl",
        [
            # Only the exact type name INTEGER makes an alias.
            "CREATE TABLE tasks (id INT PRIMARY KEY, tenant_id INTEGER NOT NULL, title TEXT)",
            "CREATE TABLE tasks (id BIGINT PRIMARY KEY, tenant_id INTEGER NOT NULL, title TEXT)",
            # A SQLite quirk: DESC in the column constraint is not an alias.
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY DESC, tenant_id INTEGER NOT NULL, "
            "title TEXT)",
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY, tenant_id INTEGER NOT NULL, "
            "title TEXT) WITHOUT ROWID",
            "CREATE TABLE tasks (id INTEGER, tenant_id INTEGER NOT NULL, title TEXT, "
            "PRIMARY KEY (id, tenant_id))",
        ],
        ids=["int", "bigint", "column-desc", "without-rowid", "composite"],
    )
    def test_is_not_provisional(self, tmp_path, ddl):
        engine = _prod(tmp_path, [ddl, SEED])
        _tasks_validator()

        with _open(engine, 42) as sandbox:
            assert sandbox.provisional_key_columns == {}


# ============================================================
# B-1  MySQL AUTO_INCREMENT: same cause, same policy
# ============================================================


@pytest.fixture
def mysql_engine():
    url = os.environ.get("MYSQL_DATABASE_URL")
    if not url:
        pytest.skip("MYSQL_DATABASE_URL is not set, so the MySQL tests are skipped.")
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tasks"))
        conn.execute(
            text(
                "CREATE TABLE tasks (id INTEGER NOT NULL AUTO_INCREMENT PRIMARY KEY, "
                "tenant_id INTEGER NOT NULL, title VARCHAR(50))"
            )
        )
        conn.execute(text(SEED))
    try:
        yield engine
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS tasks"))
        engine.dispose()


@pytest.mark.requires_db
class TestB1_MySQLAutoIncrement:
    def test_the_tenant_without_the_highest_ids_can_insert(self, mysql_engine):
        _tasks_validator()

        with _open(mysql_engine, 42) as sandbox:
            assert sandbox.provisional_key_columns == {"tasks": ["id"]}
            sandbox.execute("INSERT INTO tasks (tenant_id, title) VALUES (42, 'new')")
            changeset = sandbox.diff()
            assert changeset.is_valid
            sandbox.commit_to_production(changeset)
            (assignment,) = sandbox.assigned_keys
            assert assignment.assigned == {"id": 7}

        assert _rows(mysql_engine, "SELECT tenant_id FROM tasks WHERE id = 7") == [(42,)]

    def test_a_free_explicit_key_is_written_as_given(self, mysql_engine):
        _tasks_validator()

        with _open(mysql_engine, 42) as sandbox:
            sandbox.execute("INSERT INTO tasks (id, tenant_id, title) VALUES (100, 42, 'mine')")
            sandbox.commit_to_production(sandbox.diff())
            assert sandbox.assigned_keys == []

        assert _rows(mysql_engine, "SELECT tenant_id FROM tasks WHERE id = 100") == [(42,)]

    def test_an_explicit_key_another_tenant_holds_is_rejected(self, mysql_engine):
        _tasks_validator()

        with _open(mysql_engine, 42) as sandbox:
            sandbox.execute("INSERT INTO tasks (id, tenant_id, title) VALUES (5, 42, 'taken')")
            with pytest.raises(IntegrityViolationError):
                sandbox.commit_to_production(sandbox.diff())

        assert _rows(mysql_engine, "SELECT tenant_id FROM tasks WHERE id = 5") == [(99,)]
