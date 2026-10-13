"""
test_review_round4.py -- Round 4 of the pre-0.3.0 review (docs/REVIEW_0_3.md).

Each test turns one reproduction from docs/review-repros/p4/ into an assertion
of the behaviour that replaced it, and was written before the fix. Grouped by
finding id. File-based SQLite in tmp_path with ``PRAGMA foreign_keys=ON`` on
every connection; the naive-timestamp case needs PostgreSQL and is in
tests/test_server_backed.py.
"""

from __future__ import annotations

import decimal
from typing import Any

import pytest
from sqlalchemy import create_engine, event

from safeagentdb import (
    AgentSQLError,
    CascadeError,
    GeneratedValueError,
    SafeModel,
    ShadowDB,
    SyncError,
    UsageError,
)
from safeagentdb.diff import normalise_tenant_id, same_tenant
from safeagentdb.models import _model_registry


@pytest.fixture(autouse=True)
def _isolated_registry():
    saved = dict(_model_registry)
    _model_registry.clear()
    yield
    _model_registry.clear()
    _model_registry.update(saved)


def _prod(tmp_path, name: str, ddl: list[str], serial: bool = False):
    """A SQLite file database standing in for production, FKs on per connection.

    With ``serial``, a ``nextval()`` that returns NULL stands in for a
    PostgreSQL sequence, so a key declared ``DEFAULT (nextval(...))`` is held
    provisionally in the sandbox and assigned by production at commit.
    """
    engine = create_engine(f"sqlite:///{tmp_path / name}")

    @event.listens_for(engine, "connect")
    def _connect(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")
        if serial:
            dbapi_connection.create_function("nextval", 1, lambda _name: None)

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


TASKS = "CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, title TEXT)"


# ============================================================
# R4-1  The cascade check counted another tenant's rows as ours
# ============================================================


class TestR4_1_StrictTenantMatch:
    @pytest.mark.parametrize("value", ["042", "+42", " 42", "42 ", "42.0", 42.0, True])
    def test_a_lossy_match_is_another_tenant(self, value):
        assert same_tenant(value, 42) is False

    @pytest.mark.parametrize(
        "value", [42, "42", decimal.Decimal("42"), decimal.Decimal("42.000")]
    )
    def test_a_lossless_match_is_the_tenant(self, value):
        assert same_tenant(value, 42) is True

    def test_a_text_tenant_id_is_matched_exactly(self):
        assert same_tenant("42", "42") is True
        assert same_tenant(42, "42") is True
        assert same_tenant(" 42", "42") is False
        assert same_tenant(42, "042") is False

    def test_a_decimal_tenant_id(self):
        tenant = decimal.Decimal("42")
        assert same_tenant(42, tenant) is True
        assert same_tenant(decimal.Decimal("42.0"), tenant) is True
        assert same_tenant("042", tenant) is False
        assert same_tenant(42.0, tenant) is False

    DDL = [
        "CREATE TABLE parent (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
        "CREATE TABLE share (id INTEGER PRIMARY KEY, user_id TEXT NOT NULL,"
        " parent_id INTEGER REFERENCES parent(id) ON DELETE CASCADE)",
        "INSERT INTO parent VALUES (1, 42)",
    ]

    @pytest.mark.parametrize("other", ["042", "+42", " 42", "42.0"])
    def test_allow_cascades_never_reaches_a_lookalike_tenant(self, tmp_path, other):
        """r9: '042' was counted as tenant 42 and production deleted its row."""
        engine = _prod(
            tmp_path, "r9.db", [*self.DDL, f"INSERT INTO share VALUES (100, '{other}', 1)"]
        )
        _validator("parent", id=int, user_id=int)

        with ShadowDB(engine, tables=["parent"], tenant_id=42, allow_cascades=True) as sb:
            sb.execute("DELETE FROM parent WHERE id = 1")
            with pytest.raises(CascadeError, match="other tenant"):
                sb.commit_to_production()

        assert _rows(engine, "SELECT * FROM share") == [(100, other, 1)]
        assert _rows(engine, "SELECT * FROM parent") == [(1, 42)]

    def test_the_tenants_own_text_row_still_cascades(self, tmp_path):
        engine = _prod(tmp_path, "r9own.db", [*self.DDL, "INSERT INTO share VALUES (100, '42', 1)"])
        _validator("parent", id=int, user_id=int)

        with ShadowDB(engine, tables=["parent"], tenant_id=42, allow_cascades=True) as sb:
            sb.execute("DELETE FROM parent WHERE id = 1")
            assert sb.commit_to_production() == 1
        assert _rows(engine, "SELECT * FROM share") == []


# ============================================================
# R4-2  A NUMERIC tenant column refused an integer tenant_id
# ============================================================


class TestR4_2_NumericTenantColumn:
    @pytest.mark.parametrize(
        ("value", "python_type", "expected"),
        [
            (42, decimal.Decimal, decimal.Decimal(42)),
            ("42", decimal.Decimal, decimal.Decimal(42)),
            (" 42 ", decimal.Decimal, decimal.Decimal(42)),
            (decimal.Decimal("42"), int, 42),
            (decimal.Decimal("42.0"), int, 42),
        ],
    )
    def test_a_lossless_conversion_is_accepted(self, value, python_type, expected):
        converted = normalise_tenant_id(value, python_type)
        assert converted == expected
        assert type(converted) is python_type

    @pytest.mark.parametrize(
        ("value", "python_type"),
        [
            ("42.0", decimal.Decimal),
            (42.0, decimal.Decimal),
            (True, decimal.Decimal),
            (decimal.Decimal("42.5"), int),
            (decimal.Decimal("NaN"), int),
            (42.0, int),
        ],
    )
    def test_a_lossy_conversion_is_refused(self, value, python_type):
        with pytest.raises(ValueError):
            normalise_tenant_id(value, python_type)

    @pytest.mark.parametrize("tenant_id", [42, "42", decimal.Decimal("42")])
    def test_a_numeric_tenant_column_works(self, tmp_path, tenant_id):
        """r2: tenant_id=42 raised UsageError in __enter__."""
        engine = _prod(
            tmp_path,
            "r2.db",
            [
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY,"
                " user_id NUMERIC(10,0) NOT NULL, title TEXT)",
                "INSERT INTO tasks VALUES (1, 42, 'a'), (2, 7, 'other tenant')",
            ],
        )
        _validator("tasks", id=int, user_id=decimal.Decimal, title=str)

        with ShadowDB(engine, tables=["tasks"], tenant_id=tenant_id) as sb:
            assert sb.tenant_id == decimal.Decimal(42)
            assert sb.query("SELECT id FROM tasks") == [{"id": 1}]
            sb.execute("UPDATE tasks SET title = 'b' WHERE id = 1")
            changeset = sb.diff()
            assert changeset.is_valid is True, changeset.blocking_errors
            assert sb.commit_to_production(changeset=changeset) == 1

        assert _rows(engine, "SELECT id, title FROM tasks ORDER BY id") == [
            (1, "b"),
            (2, "other tenant"),
        ]

    def test_a_fractional_tenant_id_is_refused_for_an_integer_column(self, tmp_path):
        engine = _prod(tmp_path, "r2b.db", [TASKS])
        with pytest.raises(UsageError, match="tenant_id"):
            ShadowDB(engine, tables=["tasks"], tenant_id=decimal.Decimal("42.5")).__enter__()


# ============================================================
# R4-3  UPDATEs were not ordered by row references
# ============================================================


UNIQUE_PARENT = [
    "CREATE TABLE parent (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " code TEXT NOT NULL UNIQUE)",
    "CREATE TABLE child (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " pcode TEXT REFERENCES parent(code))",
    "INSERT INTO parent VALUES (1, 42, 'A'), (2, 42, 'C')",
    "INSERT INTO child VALUES (10, 42, 'A')",
]


class TestR4_3_UpdateOrder:
    def _run(self, tmp_path, name, statements):
        engine = _prod(tmp_path, name, UNIQUE_PARENT)
        _validator("parent", id=int, user_id=int, code=str)
        _validator("child", id=int, user_id=int, pcode=str | None)
        with ShadowDB(engine, tables=["parent", "child"], tenant_id=42) as sb:
            for statement in statements:
                sb.execute(statement)
            changeset = sb.diff()
            assert changeset.is_valid is True, changeset.blocking_errors
            written = sb.commit_to_production(changeset=changeset)
        return engine, written

    def test_a_child_is_repointed_before_its_parent_value_changes(self, tmp_path):
        """r1: the parent UPDATE ran first and production rejected it."""
        engine, written = self._run(
            tmp_path,
            "r1.db",
            [
                "UPDATE child SET pcode = 'C' WHERE id = 10",
                "UPDATE parent SET code = 'Z' WHERE id = 1",
            ],
        )
        assert written == 2
        assert _rows(engine, "SELECT * FROM parent ORDER BY id") == [(1, 42, "Z"), (2, 42, "C")]
        assert _rows(engine, "SELECT * FROM child") == [(10, 42, "C")]

    def test_a_parent_value_exists_before_a_child_insert_references_it(self, tmp_path):
        """r1 variant: the INSERT ran before the UPDATE that created 'B'."""
        engine, written = self._run(
            tmp_path,
            "r1b.db",
            [
                "UPDATE parent SET code = 'B' WHERE id = 2",
                "INSERT INTO child VALUES (11, 42, 'B')",
            ],
        )
        assert written == 2
        assert _rows(engine, "SELECT * FROM child ORDER BY id") == [(10, 42, "A"), (11, 42, "B")]

    def test_a_parent_value_exists_before_a_child_update_references_it(self, tmp_path):
        engine, written = self._run(
            tmp_path,
            "r1c.db",
            [
                "UPDATE parent SET code = 'B' WHERE id = 2",
                "UPDATE child SET pcode = 'B' WHERE id = 10",
            ],
        )
        assert written == 2
        assert _rows(engine, "SELECT * FROM child") == [(10, 42, "B")]

    def test_a_child_is_deleted_before_its_parent_value_changes(self, tmp_path):
        engine, written = self._run(
            tmp_path,
            "r1d.db",
            [
                "DELETE FROM child WHERE id = 10",
                "UPDATE parent SET code = 'Z' WHERE id = 1",
            ],
        )
        assert written == 2
        assert _rows(engine, "SELECT * FROM child") == []
        assert _rows(engine, "SELECT code FROM parent ORDER BY id") == [("Z",), ("C",)]

    def test_a_child_that_follows_its_parent_has_no_order_and_is_refused(self, tmp_path):
        """ON UPDATE CASCADE moves the child in the sandbox. One statement at a
        time, the parent must change first (the child would reference a value
        that does not exist yet) and last (it still references the old one)."""
        ddl = [
            UNIQUE_PARENT[0],
            "CREATE TABLE child (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
            " pcode TEXT REFERENCES parent(code) ON UPDATE CASCADE)",
            *UNIQUE_PARENT[2:],
        ]
        engine = _prod(tmp_path, "r1e.db", ddl)
        _validator("parent", id=int, user_id=int, code=str)
        _validator("child", id=int, user_id=int, pcode=str | None)
        with ShadowDB(
            engine, tables=["parent", "child"], tenant_id=42, allow_cascades=True
        ) as sb:
            sb.execute("UPDATE parent SET code = 'Z' WHERE id = 1")
            changeset = sb.diff()
            assert changeset.is_valid is False
            assert any("no order" in e for e in changeset.blocking_errors)
            with pytest.raises(SyncError, match="no order"):
                sb.commit_to_production()
        assert _rows(engine, "SELECT code FROM parent ORDER BY id") == [("A",), ("C",)]

    @pytest.mark.parametrize(
        ("action", "after"), [("SET NULL", None), ("SET DEFAULT", "C")]
    )
    def test_on_update_set_null_or_default_on_a_cloned_child_commits(
        self, tmp_path, action, after
    ):
        """The child is written first, so production has nothing to act on."""
        ddl = [
            UNIQUE_PARENT[0],
            "CREATE TABLE child (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
            f" pcode TEXT DEFAULT 'C' REFERENCES parent(code) ON UPDATE {action})",
            *UNIQUE_PARENT[2:],
        ]
        engine = _prod(tmp_path, "r1f.db", ddl)
        _validator("parent", id=int, user_id=int, code=str)
        _validator("child", id=int, user_id=int, pcode=str | None)
        with ShadowDB(engine, tables=["parent", "child"], tenant_id=42) as sb:
            sb.execute("UPDATE parent SET code = 'Z' WHERE id = 1")
            changeset = sb.diff()
            assert changeset.is_valid is True, changeset.blocking_errors
            assert sb.commit_to_production(changeset=changeset) == 2
        assert _rows(engine, "SELECT * FROM child") == [(10, 42, after)]


# ============================================================
# R4-4  A key set by UPDATE on the agent's own row read as generated
# ============================================================


SNOWFLAKE = 1234567890123456789  # above 2**52

ACCOUNTS = [
    "CREATE TABLE accounts (id INTEGER PRIMARY KEY DEFAULT (nextval('accounts_id_seq')),"
    " user_id INTEGER NOT NULL, name TEXT)",
    "INSERT INTO accounts VALUES (1, 42, 'existing')",
]


class TestR4_4_ExplicitKeyByUpdate:
    def test_a_key_set_by_update_is_a_supplied_value(self, tmp_path):
        """r3: production replaced the agent's id with one of its own."""
        engine = _prod(tmp_path, "r3.db", ACCOUNTS, serial=True)
        _validator("accounts", id=int, user_id=int, name=str)

        with ShadowDB(engine, tables=["accounts"], tenant_id=42) as sb:
            sb.execute("INSERT INTO accounts (user_id, name) VALUES (42, 'new')")
            sb.execute(f"UPDATE accounts SET id = {SNOWFLAKE} WHERE name = 'new'")
            changeset = sb.diff()
            (insert,) = changeset.diffs
            assert insert.pending_key_columns() == []
            assert changeset.is_valid is False
            with pytest.raises(GeneratedValueError, match="supplies a value"):
                sb.commit_to_production()

        assert _rows(engine, "SELECT id FROM accounts") == [(1,)]

    def test_an_update_that_keeps_the_generated_key_is_still_generated(self, tmp_path):
        engine = _prod(tmp_path, "r3b.db", ACCOUNTS, serial=True)
        _validator("accounts", id=int, user_id=int, name=str)

        with ShadowDB(engine, tables=["accounts"], tenant_id=42) as sb:
            sb.execute("INSERT INTO accounts (user_id, name) VALUES (42, 'new')")
            sb.execute("UPDATE accounts SET id = id, name = 'renamed' WHERE name = 'new'")
            changeset = sb.diff()
            assert changeset.is_valid is True, changeset.validate_all()
            assert sb.commit_to_production(changeset=changeset) == 1

        assert _rows(engine, "SELECT id, name FROM accounts ORDER BY id") == [
            (1, "existing"),
            (2, "renamed"),
        ]


# ============================================================
# R4-6  Delete, re-insert, then move read as a key change
# ============================================================


class TestR4_6_DeletedOriginal:
    def test_a_reused_key_moved_after_a_delete_is_not_a_key_change(self, tmp_path):
        """r6: the move of the new row 1 was blamed on the deleted original."""
        engine = _prod(tmp_path, "r6.db", [TASKS, "INSERT INTO tasks VALUES (1, 42, 'a')"])
        _validator("tasks", id=int, user_id=int, title=str)

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("DELETE FROM tasks WHERE id = 1")
            sb.execute("INSERT INTO tasks VALUES (1, 42, 'brand new')")
            sb.execute("UPDATE tasks SET id = 7 WHERE id = 1")
            changeset = sb.diff()
            assert changeset.blocking_errors == []
            assert changeset.is_valid is True
            assert sb.commit_to_production(changeset=changeset) == 2

        assert _rows(engine, "SELECT * FROM tasks") == [(7, 42, "brand new")]

    def test_an_original_moved_then_deleted_and_its_key_reused(self, tmp_path):
        """The original's key change is still reported when its row survives."""
        engine = _prod(
            tmp_path, "r6b.db", [TASKS, "INSERT INTO tasks VALUES (1, 42, 'a'), (2, 42, 'b')"]
        )
        _validator("tasks", id=int, user_id=int, title=str)

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("DELETE FROM tasks WHERE id = 1")
            sb.execute("UPDATE tasks SET id = 9 WHERE id = 2")
            changeset = sb.diff()
            assert changeset.is_valid is False
            assert any("{'id': 2}" in e for e in changeset.blocking_errors)


# ============================================================
# R4-8  ANALYZE was refused as a schema change
# ============================================================


def test_r4_8_analyze_is_not_a_schema_change(tmp_path):
    engine = _prod(tmp_path, "r5.db", [TASKS, "INSERT INTO tasks VALUES (1, 42, 'a')"])
    _validator("tasks", id=int, user_id=int, title=str)

    with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
        sb.execute("UPDATE tasks SET title = 'b' WHERE id = 1")
        sb.execute("ANALYZE")
        changeset = sb.diff()
        assert changeset.is_valid is True
        assert sb.commit_to_production(changeset=changeset) == 1


# ============================================================
# R4-9  AgentSQLError said earlier changes were kept after a rollback
# ============================================================


class TestR4_9_AgentSQLErrorMessage:
    def _engine(self, tmp_path, name):
        engine = _prod(tmp_path, name, [TASKS, "INSERT INTO tasks VALUES (1, 42, 'a')"])
        _validator("tasks", id=int, user_id=int, title=str)
        return engine

    def test_or_rollback_says_the_changes_were_discarded(self, tmp_path):
        """r7"""
        with ShadowDB(self._engine(tmp_path, "r7.db"), tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET title = 'b' WHERE id = 1")
            with pytest.raises(AgentSQLError) as excinfo:
                sb.execute("INSERT OR ROLLBACK INTO tasks VALUES (1, 42, 'dup')")
            message = str(excinfo.value)
            assert "kept" not in message
            assert "discarded" in message
            assert sb.diff().is_empty

    def test_a_plain_failure_says_the_changes_were_kept(self, tmp_path):
        with ShadowDB(self._engine(tmp_path, "r7b.db"), tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET title = 'b' WHERE id = 1")
            with pytest.raises(AgentSQLError) as excinfo:
                sb.execute("INSERT INTO tasks VALUES (1, 42, 'dup')")
            assert "kept" in str(excinfo.value)
            assert "discarded" not in str(excinfo.value)
            assert len(sb.diff().diffs) == 1
