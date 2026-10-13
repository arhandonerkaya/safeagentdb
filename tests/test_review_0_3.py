"""
test_review_0_3.py -- The findings of the pre-0.3.0 review (docs/REVIEW_0_3.md).

Each test turns one reproduction from docs/review-repros/ into an assertion of
the behaviour that replaced it. They were written before the fixes and each
one failed first against hardening-2 at 1e18b73.

Tests are grouped by finding id, in the order of the review's index. Plain
pytest, file-based SQLite in tmp_path with ``PRAGMA foreign_keys=ON`` on every
connection, as a production SQLite deployment would set it. The findings that
depend on server behaviour also have PostgreSQL versions in
tests/test_server_backed.py.
"""

from __future__ import annotations

import datetime as dt
import decimal
import json
import os
import sqlite3
import warnings
from typing import Any, Literal

import pytest
from pydantic import ValidationError
from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    create_engine,
    event,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.exc import IntegrityError, OperationalError

from safeagentdb import (
    AgentSQLError,
    CascadeError,
    ChangeSet,
    ChangesetMismatchError,
    DiffType,
    GeneratedValueError,
    RowDiff,
    RowValidationError,
    SafeAgentDBError,
    SafeModel,
    SchemaError,
    ShadowDB,
    SyncError,
    UsageError,
)
from safeagentdb.models import _model_registry


@pytest.fixture(autouse=True)
def _isolated_registry():
    saved = dict(_model_registry)
    _model_registry.clear()
    yield
    _model_registry.clear()
    _model_registry.update(saved)


def _prod(tmp_path, name: str, ddl: list[str], **engine_kwargs):
    """A SQLite file database standing in for production, FKs on per connection."""
    path = tmp_path / name
    engine = create_engine(f"sqlite:///{path}", **engine_kwargs)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as conn:
        for statement in ddl:
            conn.exec_driver_sql(statement)
    return engine


def _rows(engine, sql: str) -> list[tuple]:
    with engine.connect() as conn:
        return [tuple(r) for r in conn.exec_driver_sql(sql)]


def _validator(table: str, **fields: Any) -> type[SafeModel]:
    """Register a SafeModel for ``table`` with the given field annotations."""
    namespace = {"__table_name__": table, "__annotations__": dict(fields)}
    return type(f"V_{table}", (SafeModel,), namespace)


PARENT_CHILD = [
    "CREATE TABLE parent (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
    "CREATE TABLE child (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " parent_id INTEGER NOT NULL REFERENCES parent(id) {actions})",
]


def _parent_child(tmp_path, name, actions, rows):
    ddl = [PARENT_CHILD[0], PARENT_CHILD[1].format(actions=actions), *rows]
    return _prod(tmp_path, name, ddl)


def _register_parent_child():
    _validator("parent", id=int, user_id=int)
    _validator("child", id=int, user_id=int, parent_id=int)


# ============================================================
# P1-1  A primary-key change was committed as DELETE + INSERT
# ============================================================


class TestP1_1_KeyChanges:
    def test_a_key_change_with_an_uncloned_cascading_child_is_refused(self, tmp_path):
        """f2b: the commit deleted both children through ON DELETE CASCADE,
        where the agent's UPDATE would have re-pointed them."""
        engine = _parent_child(
            tmp_path,
            "f2b.db",
            "ON UPDATE CASCADE ON DELETE CASCADE",
            [
                "INSERT INTO parent VALUES (1, 42)",
                "INSERT INTO child VALUES (10, 42, 1), (11, 42, 1)",
            ],
        )
        _register_parent_child()

        with ShadowDB(engine, tables=["parent"], tenant_id=42) as sb:
            sb.execute("UPDATE parent SET id = 5 WHERE id = 1")
            cs = sb.diff()
            assert cs.is_valid is False
            assert any("key" in e and "application" in e for e in cs.blocking_errors)
            with pytest.raises(SyncError, match="row key"):
                sb.commit_to_production(changeset=cs)

        assert _rows(engine, "SELECT * FROM parent") == [(1, 42)]
        assert _rows(engine, "SELECT * FROM child ORDER BY id") == [(10, 42, 1), (11, 42, 1)]

    def test_a_key_change_under_on_delete_restrict_names_the_key_change(self, tmp_path):
        """f2c: the commit failed with an IntegrityViolationError about 'the
        DELETE on parent', a statement the agent never issued."""
        engine = _parent_child(
            tmp_path,
            "f2c.db",
            "ON UPDATE CASCADE ON DELETE RESTRICT",
            ["INSERT INTO parent VALUES (1, 42)", "INSERT INTO child VALUES (10, 42, 1)"],
        )
        _register_parent_child()

        with ShadowDB(engine, tables=["parent"], tenant_id=42) as sb:
            sb.execute("UPDATE parent SET id = 5 WHERE id = 1")
            with pytest.raises(SyncError) as excinfo:
                sb.commit_to_production()
        message = str(excinfo.value)
        assert "row key" in message
        assert "DELETE on 'parent'" not in message
        assert _rows(engine, "SELECT * FROM parent") == [(1, 42)]

    def test_a_key_change_with_a_cloned_child_is_refused_not_a_raw_error(self, tmp_path):
        """f2a: ended in a raw IntegrityError from the sandbox (P1-4), and would
        have been committed as DELETE + INSERT."""
        engine = _parent_child(
            tmp_path,
            "f2a.db",
            "ON UPDATE CASCADE ON DELETE CASCADE",
            ["INSERT INTO parent VALUES (1, 42)", "INSERT INTO child VALUES (10, 42, 1)"],
        )
        _register_parent_child()

        with ShadowDB(engine, tables=["parent", "child"], tenant_id=42) as sb:
            sb.execute("UPDATE parent SET id = 5 WHERE id = 1")
            # The sandbox now runs the production ON UPDATE action itself.
            assert sb.query("SELECT parent_id FROM child") == [{"parent_id": 5}]
            cs = sb.diff()
            assert cs.is_valid is False
            with pytest.raises(SyncError, match="row key"):
                sb.commit_to_production()

        assert _rows(engine, "SELECT * FROM child") == [(10, 42, 1)]

    def test_a_key_change_with_no_references_is_refused_too(self, tmp_path):
        engine = _prod(
            tmp_path,
            "plain_pk.db",
            [
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " title TEXT NOT NULL)",
                "INSERT INTO tasks VALUES (1, 42, 'one')",
            ],
        )
        _validator("tasks", id=int, user_id=int, title=str)

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET id = 7 WHERE id = 1")
            assert sb.diff().is_valid is False
            with pytest.raises(SyncError, match="row key"):
                sb.commit_to_production()
        assert _rows(engine, "SELECT * FROM tasks") == [(1, 42, "one")]

    def test_a_key_changed_and_changed_back_is_not_a_key_change(self, tmp_path):
        engine = _prod(
            tmp_path,
            "revert_pk.db",
            [
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " title TEXT NOT NULL)",
                "INSERT INTO tasks VALUES (1, 42, 'one')",
            ],
        )
        _validator("tasks", id=int, user_id=int, title=str)

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET id = 7 WHERE id = 1")
            sb.execute("UPDATE tasks SET id = 1, title = 'renamed' WHERE id = 7")
            cs = sb.diff()
            assert cs.is_valid is True
            assert cs.summary == {"INSERT": 0, "UPDATE": 1, "DELETE": 0}
            assert sb.commit_to_production() == 1
        assert _rows(engine, "SELECT * FROM tasks") == [(1, 42, "renamed")]

    def test_a_delete_and_an_insert_issued_as_such_are_not_a_key_change(self, tmp_path):
        """Only a key the agent changed with UPDATE is refused. A DELETE plus an
        INSERT the agent issued is applied as exactly that."""
        engine = _prod(
            tmp_path,
            "del_ins.db",
            [
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " title TEXT NOT NULL)",
                "INSERT INTO tasks VALUES (1, 42, 'one')",
            ],
        )
        _validator("tasks", id=int, user_id=int, title=str)

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("DELETE FROM tasks WHERE id = 1")
            sb.execute("INSERT INTO tasks VALUES (7, 42, 'one')")
            assert sb.diff().is_valid is True
            assert sb.commit_to_production() == 2
        assert _rows(engine, "SELECT * FROM tasks") == [(7, 42, "one")]

    def test_a_same_tenant_cascade_is_refused_by_default(self, tmp_path):
        """Independently of key changes: a DELETE whose ON DELETE action would
        reach rows the diff does not show is refused, even within the tenant."""
        engine = _parent_child(
            tmp_path,
            "same_tenant.db",
            "ON DELETE CASCADE",
            ["INSERT INTO parent VALUES (1, 42)", "INSERT INTO child VALUES (10, 42, 1)"],
        )
        _register_parent_child()

        with ShadowDB(engine, tables=["parent"], tenant_id=42) as sb:
            sb.execute("DELETE FROM parent WHERE id = 1")
            with pytest.raises(CascadeError, match="allow_cascades") as excinfo:
                sb.commit_to_production()
        assert excinfo.value.referencing_table == "child"
        assert excinfo.value.action == "ON DELETE CASCADE"
        assert _rows(engine, "SELECT COUNT(*) FROM child") == [(1,)]

    def test_set_null_is_a_referential_action_too(self, tmp_path):
        engine = _prod(
            tmp_path,
            "set_null.db",
            [
                PARENT_CHILD[0],
                "CREATE TABLE note (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " parent_id INTEGER REFERENCES parent(id) ON DELETE SET NULL)",
                "INSERT INTO parent VALUES (1, 42)",
                "INSERT INTO note VALUES (20, 42, 1)",
            ],
        )
        _validator("parent", id=int, user_id=int)

        with ShadowDB(engine, tables=["parent"], tenant_id=42) as sb:
            sb.execute("DELETE FROM parent WHERE id = 1")
            with pytest.raises(CascadeError, match="SET NULL"):
                sb.commit_to_production()
        assert _rows(engine, "SELECT * FROM note") == [(20, 42, 1)]

    def test_allow_cascades_permits_a_same_tenant_cascade(self, tmp_path):
        engine = _parent_child(
            tmp_path,
            "allow.db",
            "ON DELETE CASCADE",
            ["INSERT INTO parent VALUES (1, 42)", "INSERT INTO child VALUES (10, 42, 1)"],
        )
        _register_parent_child()

        with ShadowDB(engine, tables=["parent"], tenant_id=42, allow_cascades=True) as sb:
            sb.execute("DELETE FROM parent WHERE id = 1")
            assert sb.commit_to_production() == 1
        assert _rows(engine, "SELECT COUNT(*) FROM child") == [(0,)]

    def test_allow_cascades_still_refuses_another_tenant(self, tmp_path):
        engine = _parent_child(
            tmp_path,
            "allow_cross.db",
            "ON DELETE CASCADE",
            ["INSERT INTO parent VALUES (1, 42)", "INSERT INTO child VALUES (10, 99, 1)"],
        )
        _register_parent_child()

        with ShadowDB(engine, tables=["parent"], tenant_id=42, allow_cascades=True) as sb:
            sb.execute("DELETE FROM parent WHERE id = 1")
            with pytest.raises(CascadeError, match="other tenant"):
                sb.commit_to_production()
        assert _rows(engine, "SELECT COUNT(*) FROM child") == [(1,)]

    def test_a_cascade_the_diff_already_shows_needs_no_opt_in(self, tmp_path):
        """With the child cloned, the sandbox runs the cascade itself, the diff
        lists the child rows, and the children are deleted first -- so the
        parent's DELETE triggers nothing in production."""
        engine = _parent_child(
            tmp_path,
            "visible.db",
            "ON DELETE CASCADE",
            ["INSERT INTO parent VALUES (1, 42)", "INSERT INTO child VALUES (10, 42, 1)"],
        )
        _register_parent_child()

        with ShadowDB(engine, tables=["parent", "child"], tenant_id=42) as sb:
            sb.execute("DELETE FROM parent WHERE id = 1")
            cs = sb.diff()
            assert sorted((d.table, d.diff_type.value) for d in cs.diffs) == [
                ("child", "DELETE"),
                ("parent", "DELETE"),
            ]
            assert sb.commit_to_production() == 2
        assert _rows(engine, "SELECT COUNT(*) FROM parent") == [(0,)]


# ============================================================
# P1-2  All DELETEs ran before any UPDATE
# ============================================================


class TestP1_2_StatementOrder:
    ROWS = [
        "INSERT INTO parent VALUES (1, 42), (2, 42)",
        "INSERT INTO child VALUES (10, 42, 1)",
    ]

    def test_reparent_then_delete_commits(self, tmp_path):
        """f1a: failed with an IntegrityViolationError that blamed a rule the
        sandbox 'could not evaluate'."""
        engine = _parent_child(tmp_path, "f1a.db", "", self.ROWS)
        _register_parent_child()

        with ShadowDB(engine, tables=["parent", "child"], tenant_id=42) as sb:
            sb.execute("UPDATE child SET parent_id = 2 WHERE id = 10")
            sb.execute("DELETE FROM parent WHERE id = 1")
            cs = sb.diff()
            assert cs.is_valid is True
            assert sb.commit_to_production(changeset=cs) == 2

        assert _rows(engine, "SELECT * FROM parent") == [(2, 42)]
        assert _rows(engine, "SELECT * FROM child") == [(10, 42, 2)]

    @pytest.mark.parametrize("mode", ["abort", "ignore"])
    def test_reparent_then_delete_under_cascade_keeps_the_child(self, tmp_path, mode):
        """f1b: abort blamed a concurrent writer that did not exist; ignore
        destroyed the child the diff showed as updated."""
        engine = _parent_child(tmp_path, f"f1b_{mode}.db", "ON DELETE CASCADE", self.ROWS)
        _register_parent_child()

        with ShadowDB(
            engine, tables=["parent", "child"], tenant_id=42, on_conflict=mode
        ) as sb:
            sb.execute("UPDATE child SET parent_id = 2 WHERE id = 10")
            sb.execute("DELETE FROM parent WHERE id = 1")
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                assert sb.commit_to_production() == 2
            assert sb.skipped_conflicts == []

        assert _rows(engine, "SELECT * FROM child") == [(10, 42, 2)]

    def test_inserts_then_updates_then_deletes(self):
        from safeagentdb.sync import _order_diffs

        meta = MetaData()
        Table("parent", meta, Column("id", Integer, primary_key=True))
        Table(
            "child",
            meta,
            Column("id", Integer, primary_key=True),
            Column("parent_id", Integer, ForeignKey("parent.id")),
        )
        diffs = [
            RowDiff("parent", DiffType.DELETE, {"id": 1}, old={"id": 1}),
            RowDiff("child", DiffType.DELETE, {"id": 9}, old={"id": 9, "parent_id": 1}),
            RowDiff("child", DiffType.UPDATE, {"id": 10}, old={"id": 10}, new={"id": 10}),
            RowDiff("child", DiffType.INSERT, {"id": 11}, new={"id": 11, "parent_id": 3}),
            RowDiff("parent", DiffType.INSERT, {"id": 3}, new={"id": 3}),
        ]
        ordered = [
            (d.diff_type.value, d.table, d.pk["id"])
            for d in _order_diffs(ChangeSet(diffs=diffs), meta)
        ]
        assert ordered == [
            ("INSERT", "parent", 3),
            ("INSERT", "child", 11),
            ("UPDATE", "child", 10),
            ("DELETE", "child", 9),
            ("DELETE", "parent", 1),
        ]


# ============================================================
# P1-3  A JSON change Python considers equal was never written
# ============================================================


JSON_DOCS = [
    "CREATE TABLE docs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, data JSON)",
    """INSERT INTO docs VALUES (1, 42, '{"enabled": 1}'), (2, 42, '{"a": 1}')""",
]


class TestP1_3_JsonEquality:
    def test_one_to_true_is_a_change_and_is_written(self, tmp_path):
        """f4 row 1: shown as an UPDATE with no changed columns, never written."""
        engine = _prod(tmp_path, "f4a.db", JSON_DOCS)
        _validator("docs", id=int, user_id=int, data=Any)

        with ShadowDB(engine, tables=["docs"], tenant_id=42) as sb:
            sb.execute(
                "UPDATE docs SET data = :v WHERE id = 1", {"v": '{"enabled": true}'}
            )
            cs = sb.diff()
            (diff,) = cs.diffs
            assert diff.changed_columns() == ["data"]
            assert sb.commit_to_production(changeset=cs) == 1

        (stored,) = _rows(engine, "SELECT data FROM docs WHERE id = 1")[0]
        assert json.loads(stored) == {"enabled": True}
        assert json.loads(stored)["enabled"] is True

    def test_a_formatting_only_rewrite_is_not_a_change(self, tmp_path):
        """f4 row 2: shown as an UPDATE with nothing in it."""
        engine = _prod(tmp_path, "f4b.db", JSON_DOCS)
        _validator("docs", id=int, user_id=int, data=Any)

        with ShadowDB(engine, tables=["docs"], tenant_id=42) as sb:
            sb.execute("UPDATE docs SET data = :v WHERE id = 2", {"v": '{"a":1}'})
            assert sb.diff().is_empty

    def test_one_and_one_point_zero_differ(self):
        diff = RowDiff(
            "docs",
            DiffType.UPDATE,
            {"id": 1},
            old={"id": 1, "data": '{"a": 1}'},
            new={"id": 1, "data": '{"a": 1.0}'},
            converted_columns={"data": "json"},
        )
        assert diff.changed_columns() == ["data"]


# ============================================================
# P1-4  With SQLite production, the sandbox dropped FK actions
# ============================================================


class TestP1_4_SqliteReferentialActions:
    def _engine(self, tmp_path):
        return _prod(
            tmp_path,
            "f3.db",
            [
                PARENT_CHILD[0],
                "CREATE TABLE child (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " parent_id INTEGER REFERENCES parent(id)"
                " ON DELETE CASCADE ON UPDATE CASCADE)",
                "CREATE TABLE note (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " parent_id INTEGER REFERENCES parent(id) ON DELETE SET NULL)",
                "INSERT INTO parent VALUES (1, 42)",
                "INSERT INTO child VALUES (10, 42, 1)",
                "INSERT INTO note VALUES (20, 42, 1)",
            ],
        )

    def test_the_sandbox_keeps_the_actions(self, tmp_path):
        engine = self._engine(tmp_path)
        with ShadowDB(engine, tables=["parent", "child", "note"], tenant_id=42) as sb:
            ddl = sb.query("SELECT sql FROM sqlite_master WHERE name = 'child'")[0]["sql"]
            assert "ON DELETE CASCADE" in ddl
            assert "ON UPDATE CASCADE" in ddl

    def test_a_delete_production_accepts_is_accepted_in_the_sandbox(self, tmp_path):
        """f3: the sandbox raised IntegrityError for a delete production takes."""
        engine = self._engine(tmp_path)
        with ShadowDB(engine, tables=["parent", "child", "note"], tenant_id=42) as sb:
            sb.execute("DELETE FROM parent WHERE id = 1")
            assert sb.query("SELECT * FROM child") == []
            assert sb.query("SELECT parent_id FROM note") == [{"parent_id": None}]


# ============================================================
# P1-5  Any integer >= 2**52 was treated as a placeholder key
# ============================================================


SNOWFLAKE = 1_234_567_890_123_456_789


class TestP1_5_LargeIntegers:
    def _engine(self, tmp_path, name):
        return _prod(
            tmp_path,
            name,
            [
                "CREATE TABLE accounts (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " external_id BIGINT NOT NULL, name TEXT NOT NULL)",
                f"INSERT INTO accounts VALUES (1, 42, {SNOWFLAKE}, 'old')",
            ],
        )

    def test_an_update_beside_a_large_integer_commits(self, tmp_path):
        """f5: BLOCKED, then GeneratedValueError naming external_id."""
        engine = self._engine(tmp_path, "f5a.db")
        _validator("accounts", id=int, user_id=int, external_id=int, name=str)

        with ShadowDB(engine, tables=["accounts"], tenant_id=42) as sb:
            sb.execute("UPDATE accounts SET name = 'new' WHERE id = 1")
            cs = sb.diff()
            assert cs.is_valid is True, cs.validate_all()
            assert sb.commit_to_production(changeset=cs) == 1
        assert _rows(engine, "SELECT external_id, name FROM accounts") == [
            (SNOWFLAKE, "new")
        ]

    def test_an_insert_carrying_a_large_integer_commits(self, tmp_path):
        engine = self._engine(tmp_path, "f5b.db")
        _validator("accounts", id=int, user_id=int, external_id=int, name=str)

        with ShadowDB(engine, tables=["accounts"], tenant_id=42) as sb:
            sb.execute(f"INSERT INTO accounts VALUES (2, 42, {SNOWFLAKE + 1}, 'two')")
            assert sb.diff().is_valid is True
            assert sb.commit_to_production() == 1

    def test_a_row_reinserted_under_a_custom_row_key_cannot_carry_a_placeholder(
        self, tmp_path, monkeypatch
    ):
        """Suspected in the review: deleting and re-inserting a row under the
        same custom row_key gives an UPDATE whose generated id is a placeholder,
        which would be written to production as a real id."""
        from sqlalchemy import DefaultClause

        import safeagentdb.sandbox as sandbox_module
        from safeagentdb.engine import reflect_tables as real_reflect

        engine = _prod(
            tmp_path,
            "rowkey_serial.db",
            [
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " slug TEXT NOT NULL, title TEXT NOT NULL)",
                "INSERT INTO tasks VALUES (1, 42, 'a', 'one')",
            ],
        )

        def patched(source_engine, table_names):
            meta = real_reflect(source_engine, table_names)
            meta.tables["tasks"].c.id.server_default = DefaultClause(
                text("nextval('tasks_id_seq'::regclass)")
            )
            return meta

        monkeypatch.setattr(sandbox_module, "reflect_tables", patched)
        _validator("tasks", id=int, user_id=int, slug=str, title=str)

        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, row_key={"tasks": ["user_id", "slug"]}
        ) as sb:
            sb.execute("DELETE FROM tasks WHERE slug = 'a'")
            sb.execute("INSERT INTO tasks (user_id, slug, title) VALUES (42, 'a', 'again')")
            cs = sb.diff()
            assert cs.is_valid is False
            message = " ".join(m for _, ok, m in cs.validate_all() if not ok)
            assert "id" in message and "production" in message
            # The cause is the re-insert, not a reference to a new parent row.
            assert "has not assigned yet" not in message
            assert "re-inserted" in message
            with pytest.raises(GeneratedValueError):
                sb.commit_to_production()
        assert _rows(engine, "SELECT id, title FROM tasks") == [(1, "one")]


# ============================================================
# P1-6 / A3  The fingerprint collided across types
# ============================================================


class TestP1_6_Fingerprint:
    def _engine(self, tmp_path, name):
        return _prod(
            tmp_path,
            name,
            [
                "CREATE TABLE files (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " payload BLOB)",
                "INSERT INTO files VALUES (1, 42, NULL)",
            ],
        )

    def test_bytes_and_their_repr_do_not_share_a_fingerprint(self, tmp_path):
        """f6: b'x' and the text "b'x'" hashed the same."""
        engine = self._engine(tmp_path, "f6a.db")
        _validator("files", id=int, user_id=int, payload=Any)

        with ShadowDB(engine, tables=["files"], tenant_id=42) as sb:
            sb.execute("UPDATE files SET payload = :v WHERE id = 1", {"v": b"x"})
            reviewed = sb.diff()
            sb.execute("UPDATE files SET payload = :v WHERE id = 1", {"v": "b'x'"})
            assert sb.diff().fingerprint != reviewed.fingerprint
            with pytest.raises(ChangesetMismatchError):
                sb.commit_to_production(changeset=reviewed)

    def test_a_bind_time_error_at_commit_stays_in_the_hierarchy(self, tmp_path):
        """A3: the commit escaped as a raw sqlalchemy StatementError."""
        engine = self._engine(tmp_path, "f6b.db")
        _validator("files", id=int, user_id=int, payload=Any)

        with ShadowDB(engine, tables=["files"], tenant_id=42) as sb:
            sb.execute("UPDATE files SET payload = :v WHERE id = 1", {"v": "b'x'"})
            with pytest.raises(SafeAgentDBError) as excinfo:
                sb.commit_to_production()
        assert excinfo.value.__cause__ is not None
        assert _rows(engine, "SELECT payload FROM files") == [(None,)]

    @pytest.mark.parametrize(
        "a, b",
        [(1, True), (1, 1.0), (1, "1"), (b"x", "b'x'"), (None, "None"), ({"a": 1}, '{"a": 1}')],
    )
    def test_values_of_different_types_never_collide(self, a, b):
        def changeset(value):
            return ChangeSet(
                diffs=[RowDiff("t", DiffType.INSERT, {"id": 1}, new={"id": 1, "v": value})]
            )

        assert changeset(a).fingerprint != changeset(b).fingerprint


# ============================================================
# P2-1  False ConflictError for another text format (SQLite)
# ============================================================


FORMATS = [
    "CREATE TABLE r (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " note TEXT NOT NULL, ts DATETIME, flag BOOLEAN)",
    "INSERT INTO r (id, user_id, note, ts) VALUES (11, 42, 'n', '2024-01-01 10:00:00')",
    "INSERT INTO r (id, user_id, note, ts) VALUES (12, 42, 'n', '2024-01-01T10:00:00')",
    "INSERT INTO r (id, user_id, note, flag) VALUES (14, 42, 'n', 'true')",
]


class TestP2_1_SqliteTextFormats:
    @pytest.mark.parametrize("row", [11, 12, 14])
    def test_a_delete_commits(self, tmp_path, row):
        """t1 (c): ConflictError '... DELETE matched nothing'."""
        engine = _prod(tmp_path, f"t1_{row}.db", FORMATS)
        _validator("r", id=int, user_id=int, note=str, ts=Any, flag=Any)

        with ShadowDB(engine, tables=["r"], tenant_id=42) as sb:
            sb.execute(f"DELETE FROM r WHERE id = {row}")
            assert sb.commit_to_production() == 1
        assert _rows(engine, f"SELECT COUNT(*) FROM r WHERE id = {row}") == [(0,)]

    @pytest.mark.parametrize(
        "row, column, value", [(11, "ts", "2025-03-01 08:30:00"), (14, "flag", 0)]
    )
    def test_an_update_of_the_column_commits(self, tmp_path, row, column, value):
        """t2: ConflictError '... the UPDATE matched nothing'."""
        engine = _prod(tmp_path, f"t2_{row}.db", FORMATS)
        _validator("r", id=int, user_id=int, note=str, ts=Any, flag=Any)

        with ShadowDB(engine, tables=["r"], tenant_id=42) as sb:
            sb.execute(f"UPDATE r SET {column} = :v WHERE id = {row}", {"v": value})
            assert sb.commit_to_production() == 1


# ============================================================
# P2-2  JSON scalars changed type in the sandbox
# ============================================================


class TestP2_2_JsonScalars:
    def test_copied_json_strings_stay_strings(self, tmp_path):
        """t5: '"123"' was written back as the number 123 and '"null"' as null."""
        engine = _prod(
            tmp_path,
            "t5.db",
            [
                "CREATE TABLE prefs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " code JSON)",
                """INSERT INTO prefs VALUES (1, 42, '"123"'), (2, 42, '"null"'),"""
                """ (3, 42, 'true')""",
            ],
        )
        _validator("prefs", id=int, user_id=int, code=Any)

        with ShadowDB(engine, tables=["prefs"], tenant_id=42) as sb:
            sb.execute("INSERT INTO prefs SELECT id + 10, user_id, code FROM prefs")
            cs = sb.diff()
            logical = {d.pk["id"]: d.logical_row(d.new)["code"] for d in cs.diffs}
            assert logical == {11: "123", 12: "null", 13: True}
            assert sb.commit_to_production(changeset=cs) == 3

        stored = dict(_rows(engine, "SELECT id, code FROM prefs WHERE id > 10"))
        assert {k: json.loads(v) for k, v in stored.items()} == {
            11: "123",
            12: "null",
            13: True,
        }

    def test_a_typed_validator_sees_the_json_type(self, tmp_path):
        """t3: a row holding JSON true was BLOCKED for an edit to another column,
        then the commit raised pydantic.ValidationError."""
        engine = _prod(
            tmp_path,
            "t3.db",
            [
                "CREATE TABLE prefs (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " note TEXT NOT NULL, code JSON, enabled JSON)",
                """INSERT INTO prefs VALUES (1, 42, 'n', '"00123"', 'true')""",
            ],
        )
        _validator(
            "prefs", id=int, user_id=int, note=str, code=str | None, enabled=bool | None
        )

        with ShadowDB(engine, tables=["prefs"], tenant_id=42) as sb:
            sb.execute("UPDATE prefs SET note = 'edited' WHERE id = 1")
            cs = sb.diff()
            assert cs.diffs[0].changed_columns() == ["note"]
            assert cs.is_valid is True, cs.validate_all()
            assert sb.commit_to_production() == 1
        assert _rows(engine, "SELECT code, enabled FROM prefs") == [('"00123"', "true")]

    def test_text_that_is_not_json_is_refused_clearly(self, tmp_path):
        engine = _prod(tmp_path, "badjson.db", JSON_DOCS)
        _validator("docs", id=int, user_id=int, data=Any)

        with ShadowDB(engine, tables=["docs"], tenant_id=42) as sb:
            sb.execute("UPDATE docs SET data = 'hello' WHERE id = 1")
            cs = sb.diff()
            assert cs.is_valid is False
            message = cs.validate_all()[0][2]
            assert "not valid JSON" in message
            with pytest.raises(SyncError, match="not valid JSON"):
                sb.commit_to_production()


# ============================================================
# P2-3  A datetime's UTC offset was dropped
# ============================================================


class TestP2_3_TimeZones:
    def _engine(self, tmp_path, name):
        return _prod(
            tmp_path,
            name,
            [
                "CREATE TABLE ev (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " note TEXT NOT NULL, at DATETIME)",
                "INSERT INTO ev VALUES (1, 42, 'n', '2024-01-01 10:00:00+02:00')",
                "INSERT INTO ev VALUES (2, 42, 'n', '2024-01-01T10:00:00Z')",
            ],
        )

    def test_the_sandbox_shows_the_offset(self, tmp_path):
        engine = self._engine(tmp_path, "t4a.db")
        with ShadowDB(engine, tables=["ev"], tenant_id=42) as sb:
            rows = {r["id"]: r["at"] for r in sb.query("SELECT id, at FROM ev")}
        assert "+02:00" in rows[1]
        assert rows[2].endswith(("Z", "+00:00"))

    def test_a_copied_timestamp_keeps_its_instant_and_offset(self, tmp_path):
        """t5: the copy landed as '2024-01-01 10:00:00.000000', two hours off."""
        engine = self._engine(tmp_path, "t4b.db")
        _validator("ev", id=int, user_id=int, note=str, at=dt.datetime)

        with ShadowDB(engine, tables=["ev"], tenant_id=42) as sb:
            sb.execute("INSERT INTO ev SELECT id + 10, user_id, note, at FROM ev")
            cs = sb.diff()
            assert cs.is_valid is True, cs.validate_all()
            assert sb.commit_to_production() == 2

        stored = dict(_rows(engine, "SELECT id, at FROM ev"))
        utc = dt.timezone.utc
        expected = {
            11: dt.datetime(2024, 1, 1, 8, 0, tzinfo=utc),
            12: dt.datetime(2024, 1, 1, 10, 0, tzinfo=utc),
        }
        for key, instant in expected.items():
            parsed = dt.datetime.fromisoformat(stored[key].replace("Z", "+00:00"))
            assert parsed.utcoffset() is not None, stored[key]
            assert parsed == instant
        assert dt.datetime.fromisoformat(stored[11]).utcoffset() == dt.timedelta(hours=2)

    def test_an_edit_elsewhere_leaves_the_stored_text_alone(self, tmp_path):
        engine = self._engine(tmp_path, "t4c.db")
        _validator("ev", id=int, user_id=int, note=str, at=dt.datetime)

        with ShadowDB(engine, tables=["ev"], tenant_id=42) as sb:
            sb.execute("UPDATE ev SET note = 'x'")
            cs = sb.diff()
            assert all(d.changed_columns() == ["note"] for d in cs.diffs)
            assert sb.commit_to_production() == 2
        assert _rows(engine, "SELECT at FROM ev ORDER BY id") == [
            ("2024-01-01 10:00:00+02:00",),
            ("2024-01-01T10:00:00Z",),
        ]

    def test_an_agent_written_offset_is_preserved(self, tmp_path):
        engine = self._engine(tmp_path, "t4d.db")
        _validator("ev", id=int, user_id=int, note=str, at=dt.datetime)

        with ShadowDB(engine, tables=["ev"], tenant_id=42) as sb:
            sb.execute("UPDATE ev SET at = '2025-06-01 12:00:00-05:00' WHERE id = 1")
            assert sb.commit_to_production() == 1
        (stored,) = _rows(engine, "SELECT at FROM ev WHERE id = 1")[0]
        parsed = dt.datetime.fromisoformat(stored)
        assert parsed.utcoffset() == dt.timedelta(hours=-5)
        assert parsed == dt.datetime(2025, 6, 1, 17, 0, tzinfo=dt.timezone.utc)

    def test_a_reformatted_timestamp_is_not_a_change(self, tmp_path):
        engine = self._engine(tmp_path, "t4e.db")
        with ShadowDB(engine, tables=["ev"], tenant_id=42) as sb:
            sb.execute("UPDATE ev SET at = '2024-01-01T10:00:00+02:00' WHERE id = 1")
            assert sb.diff().is_empty

    def test_text_that_is_not_a_timestamp_is_refused_clearly(self, tmp_path):
        engine = self._engine(tmp_path, "t4f.db")
        _validator("ev", id=int, user_id=int, note=str, at=Any)

        with ShadowDB(engine, tables=["ev"], tenant_id=42) as sb:
            sb.execute("UPDATE ev SET at = 'next tuesday' WHERE id = 1")
            cs = sb.diff()
            assert cs.is_valid is False
            assert "not a valid timestamp" in cs.validate_all()[0][2]
            with pytest.raises(SyncError, match="not a valid timestamp"):
                sb.commit_to_production()


# ============================================================
# A1  Errors while __enter__ reads the schema were not wrapped
# ============================================================


TASKS = [
    "CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " title TEXT NOT NULL)",
    "INSERT INTO tasks VALUES (1, 42, 'one')",
]


def _tasks(tmp_path, name, **kwargs):
    engine = _prod(tmp_path, name, TASKS, **kwargs)
    _validator("tasks", id=int, user_id=int, title=str)
    return engine


class TestA1_EnterErrors:
    def test_an_unknown_table_is_a_schema_error(self, tmp_path):
        engine = _tasks(tmp_path, "a1a.db")
        with pytest.raises(SchemaError, match="taskz") as excinfo:
            ShadowDB(engine, tables=["taskz"], tenant_id=42).__enter__()
        assert excinfo.value.__cause__ is not None

    def test_an_unreachable_database_is_a_schema_error(self, tmp_path):
        bad = create_engine("sqlite:///" + os.path.join(str(tmp_path), "missing", "x.db"))
        with pytest.raises(SchemaError) as excinfo:
            ShadowDB(bad, tables=["tasks"], tenant_id=42).__enter__()
        assert isinstance(excinfo.value.__cause__, OperationalError)


# ============================================================
# A2  Commit-time database errors were wrapped only sometimes
# ============================================================


class TestA2_CommitErrors:
    def test_an_insert_while_production_is_locked(self, tmp_path):
        """e1: the read before an INSERT escaped as a raw OperationalError."""
        engine = _tasks(tmp_path, "a2.db", connect_args={"timeout": 0.2})
        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("INSERT INTO tasks VALUES (2, 42, 'two')")
            holder = sqlite3.connect(engine.url.database, timeout=0)
            holder.execute("BEGIN EXCLUSIVE")
            try:
                with pytest.raises(SyncError) as excinfo:
                    sb.commit_to_production()
            finally:
                holder.rollback()
                holder.close()
        assert isinstance(excinfo.value.__cause__, OperationalError)

    def test_a_failure_to_start_the_transaction(self, tmp_path, monkeypatch):
        engine = _tasks(tmp_path, "a2b.db")
        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET title = 'x'")

            def refuse():
                raise OperationalError("BEGIN", {}, Exception("server went away"))

            monkeypatch.setattr(engine, "begin", refuse)
            with pytest.raises(SyncError, match="server went away"):
                sb.commit_to_production()


# ============================================================
# A4  Raw errors after the agent changed the sandbox schema
# ============================================================


class TestA4_SchemaChanges:
    def test_a_renamed_column(self, tmp_path):
        engine = _tasks(tmp_path, "a4a.db")
        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("ALTER TABLE tasks RENAME COLUMN title TO name")
            with pytest.raises(SchemaError, match="schema"):
                sb.diff()
            with pytest.raises(SchemaError, match="schema"):
                sb.commit_to_production()

    def test_a_dropped_table(self, tmp_path):
        engine = _tasks(tmp_path, "a4b.db")
        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("DROP TABLE tasks")
            with pytest.raises(SchemaError, match="tasks"):
                sb.diff()


# ============================================================
# A5  execute()/query() passed driver errors through
# ============================================================


class TestA5_AgentSQL:
    def test_a_decimal_parameter(self, tmp_path):
        engine = _tasks(tmp_path, "a5a.db")
        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute(
                "UPDATE tasks SET title = :v WHERE id = 1", {"v": decimal.Decimal("1.5")}
            )
            assert sb.query("SELECT title FROM tasks") == [{"title": "1.5"}]

    def test_a_json_literal_without_params(self, tmp_path):
        engine = _tasks(tmp_path, "a5b.db")
        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("""UPDATE tasks SET title = '{"a":1}' WHERE id = 1""")
            assert sb.query("SELECT title FROM tasks") == [{"title": '{"a":1}'}]

    def test_query_on_a_statement_without_rows(self, tmp_path):
        engine = _tasks(tmp_path, "a5c.db")
        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            with pytest.raises(AgentSQLError, match="execute"):
                sb.query("UPDATE tasks SET title = 'x' WHERE id = 1")

    def test_a_constraint_violation_is_an_agent_sql_error(self, tmp_path):
        engine = _tasks(tmp_path, "a5d.db")
        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET title = 'kept' WHERE id = 1")
            with pytest.raises(AgentSQLError) as excinfo:
                sb.execute("INSERT INTO tasks VALUES (1, 42, 'dup')")
            assert isinstance(excinfo.value.__cause__, IntegrityError)
            # Earlier work survives the failed statement.
            assert sb.query("SELECT title FROM tasks") == [{"title": "kept"}]

    def test_a_syntax_error(self, tmp_path):
        engine = _tasks(tmp_path, "a5e.db")
        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            with pytest.raises(AgentSQLError):
                sb.execute("UPDTE tasks")


# ============================================================
# A6  Misuse errors were plain Python exceptions
# ============================================================


class TestA6_Misuse:
    def test_use_before_enter(self, tmp_path):
        engine = _tasks(tmp_path, "a6a.db")
        sb = ShadowDB(engine, tables=["tasks"], tenant_id=42)
        for call in (lambda: sb.execute("SELECT 1"), sb.diff, sb.commit_to_production):
            with pytest.raises(UsageError):
                call()
        # Compatibility: code that caught RuntimeError keeps working.
        assert issubclass(UsageError, RuntimeError)

    def test_a_bad_on_conflict(self, tmp_path):
        engine = _tasks(tmp_path, "a6b.db")
        with pytest.raises(UsageError, match="on_conflict"):
            ShadowDB(engine, tables=["tasks"], tenant_id=42, on_conflict="skip")

    def test_a_changeset_that_is_not_a_changeset(self, tmp_path):
        engine = _tasks(tmp_path, "a6c.db")
        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            with pytest.raises(UsageError, match="ChangeSet"):
                sb.commit_to_production(changeset={"x": 1})

    def test_commit_time_validation_is_in_the_hierarchy(self, tmp_path):
        engine = _prod(
            tmp_path,
            "a6d.db",
            [
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
                " status TEXT NOT NULL)",
                "INSERT INTO tasks VALUES (1, 42, 'todo')",
            ],
        )
        _validator("tasks", id=int, user_id=int, status=Literal["todo", "done"])
        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET status = 'yolo'")
            with pytest.raises(RowValidationError) as excinfo:
                sb.commit_to_production()
        assert isinstance(excinfo.value.__cause__, ValidationError)
        assert excinfo.value.table == "tasks"
        assert excinfo.value.row_key == {"id": 1}


# ============================================================
# B1  A string tenant_id made every write a "Tenant breach"
# ============================================================


class TestB1_TenantIdType:
    def test_a_string_tenant_id_for_an_integer_column(self, tmp_path):
        """m1: every write was reported as a tenant breach."""
        engine = _prod(
            tmp_path,
            "m1.db",
            [
                TASKS[0],
                "INSERT INTO tasks VALUES (1, 42, 'one'), (2, 42, 'two'), (3, 99, 'theirs')",
            ],
        )
        _validator("tasks", id=int, user_id=int, title=str)

        with ShadowDB(engine, tables=["tasks"], tenant_id="42") as sb:
            assert sb.tenant_id == 42
            sb.execute("UPDATE tasks SET title = 'x' WHERE id = 1")
            sb.execute("DELETE FROM tasks WHERE id = 2")
            cs = sb.diff()
            assert cs.blocking_errors == []
            assert sb.commit_to_production(changeset=cs) == 2
        assert _rows(engine, "SELECT * FROM tasks ORDER BY id") == [
            (1, 42, "x"),
            (3, 99, "theirs"),
        ]

    def test_a_tenant_id_that_cannot_be_converted(self, tmp_path):
        engine = _tasks(tmp_path, "m1b.db")
        with pytest.raises(UsageError, match="tenant_id"):
            ShadowDB(engine, tables=["tasks"], tenant_id="forty-two").__enter__()


# ============================================================
# B2  A self-referencing table was written in key order
# ============================================================


EMP = [
    "CREATE TABLE emp (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " manager_id INTEGER REFERENCES emp(id))",
    "INSERT INTO emp VALUES (1, 42, NULL), (2, 42, 1)",
]


class TestB2_SelfReference:
    def _run(self, tmp_path, name, statements):
        engine = _prod(tmp_path, name, EMP)
        _validator("emp", id=int, user_id=int, manager_id=int | None)
        with ShadowDB(engine, tables=["emp"], tenant_id=42) as sb:
            for statement in statements:
                sb.execute(statement)
            cs = sb.diff()
            assert cs.is_valid is True, cs.blocking_errors
            written = sb.commit_to_production(changeset=cs)
        return engine, written

    def test_deleting_a_manager_and_their_report(self, tmp_path):
        engine, written = self._run(
            tmp_path,
            "m2a.db",
            ["DELETE FROM emp WHERE id = 2", "DELETE FROM emp WHERE id = 1"],
        )
        assert written == 2
        assert _rows(engine, "SELECT * FROM emp") == []

    def test_inserting_a_report_keyed_below_their_manager(self, tmp_path):
        engine, written = self._run(
            tmp_path,
            "m2b.db",
            ["INSERT INTO emp VALUES (20, 42, NULL)", "INSERT INTO emp VALUES (10, 42, 20)"],
        )
        assert written == 2
        assert _rows(engine, "SELECT * FROM emp WHERE id >= 10 ORDER BY id") == [
            (10, 42, 20),
            (20, 42, None),
        ]

    def test_a_real_cycle_is_refused_clearly(self, tmp_path):
        engine = _prod(tmp_path, "m2c.db", EMP)
        _validator("emp", id=int, user_id=int, manager_id=int | None)
        with ShadowDB(engine, tables=["emp"], tenant_id=42) as sb:
            sb.execute("INSERT INTO emp VALUES (30, 42, NULL)")
            sb.execute("INSERT INTO emp VALUES (31, 42, 30)")
            sb.execute("UPDATE emp SET manager_id = 31 WHERE id = 30")
            cs = sb.diff()
            assert cs.is_valid is False
            assert any("cycle" in e for e in cs.blocking_errors)
            with pytest.raises(SafeAgentDBError, match="cycle"):
                sb.commit_to_production()
        assert _rows(engine, "SELECT COUNT(*) FROM emp") == [(2,)]


# ============================================================
# B7  The validation message named only the field
# ============================================================


def test_b7_the_validation_message_says_why(tmp_path):
    engine = _prod(
        tmp_path,
        "m3.db",
        [
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
            " title TEXT NOT NULL, status TEXT NOT NULL)",
            "INSERT INTO tasks VALUES (1, 42, 'one', 'todo')",
        ],
    )
    _validator("tasks", id=int, user_id=int, title=str, status=Literal["todo", "done"])
    with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
        sb.execute("UPDATE tasks SET status = 'yolo' WHERE id = 1")
        message = sb.diff().validate_all()[0][2]
    assert "status" in message
    assert "'todo' or 'done'" in message
    assert "yolo" in message


# ============================================================
# C1-C3  API usage
# ============================================================


class TestC_Lifecycle:
    def test_c1_diff_after_a_commit(self, tmp_path):
        engine = _tasks(tmp_path, "c1.db")
        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET title = 'x' WHERE id = 1")
            assert sb.commit_to_production() == 1
            with pytest.raises(UsageError, match="committed"):
                sb.diff()

    def test_c2_entering_an_open_sandbox(self, tmp_path):
        engine = _tasks(tmp_path, "c2.db")
        sb = ShadowDB(engine, tables=["tasks"], tenant_id=42)
        with sb:
            sb.execute("UPDATE tasks SET title = 'before' WHERE id = 1")
            first = sb.sandbox_engine
            with pytest.raises(UsageError, match="already open"):
                sb.__enter__()
            assert sb.sandbox_engine is first
            assert sb.query("SELECT title FROM tasks") == [{"title": "before"}]

    def test_c3_reentering_a_committed_sandbox(self, tmp_path):
        engine = _tasks(tmp_path, "c3.db")
        sb = ShadowDB(engine, tables=["tasks"], tenant_id=42)
        with sb:
            sb.execute("UPDATE tasks SET title = 'x'")
            sb.commit_to_production()
        with pytest.raises(UsageError, match="committed"):
            sb.__enter__()

    def test_c3_reentering_an_uncommitted_sandbox_takes_a_fresh_clone(self, tmp_path):
        engine = _tasks(tmp_path, "c3b.db")
        sb = ShadowDB(engine, tables=["tasks"], tenant_id=42)
        with sb:
            sb.execute("UPDATE tasks SET title = 'discarded'")
        with sb:
            assert sb.query("SELECT title FROM tasks") == [{"title": "one"}]
            sb.execute("UPDATE tasks SET title = 'kept'")
            assert sb.commit_to_production() == 1
        assert _rows(engine, "SELECT title FROM tasks") == [("kept",)]


# ============================================================
# Suspected: cascade check SQL on MySQL; MySQL TINYINT
# ============================================================


def test_the_cascade_check_quotes_identifiers_for_the_dialect():
    """The check built SQL with double-quoted identifiers, which MySQL without
    ANSI_QUOTES reads as string literals."""
    from safeagentdb.engine import CascadeReference
    from safeagentdb.sync import _referencing_rows_statement

    reference = CascadeReference(
        child_table="child",
        child_columns=("parent_id",),
        parent_table="parent",
        parent_columns=("id",),
        on_delete="CASCADE",
        on_update=None,
        child_in_sandbox=False,
    )
    for with_tenant in (True, False):
        stmt = _referencing_rows_statement(
            reference, [1], "user_id" if with_tenant else None
        )
        sql = str(stmt.compile(dialect=mysql.dialect()))
        assert "`child`" in sql or " child" in sql
        assert '"child"' not in sql


def test_mysql_tinyint_is_an_integer_in_the_sandbox():
    from sqlalchemy import Integer as SAInteger

    from safeagentdb.engine import clone_schema_to_sandbox, create_sandbox_engine

    meta = MetaData()
    Table(
        "settings",
        meta,
        Column("id", Integer, primary_key=True),
        Column("active", mysql.TINYINT(1)),
    )
    sandbox_meta = clone_schema_to_sandbox(meta, create_sandbox_engine())
    assert isinstance(sandbox_meta.tables["settings"].c.active.type, SAInteger)
    assert "settings" not in sandbox_meta.info["converted_columns"]
