"""
test_mega.py -- Exhaustive test suite for SafeAgentDB v0.1.0.

Covers every public class, method, property, and edge case.
Run with: python -m pytest tests/test_mega.py -v

Sections:
  A. Imports & public API surface
  B. SafeModel & validator registry
  C. ShadowDB context manager lifecycle
  D. execute() and query()
  E. diff() -- INSERT, UPDATE, DELETE detection
  F. ChangeSet properties & display methods
  G. RowDiff introspection
  H. commit_to_production() -- happy path
  I. Pydantic validation gate -- rejects bad data
  J. Tenant isolation -- blocks cross-tenant writes
  K. Atomic rollback -- partial failure aborts everything
  L. Double-commit guard
  M. Non-TTY plain-text fallback
  N. Multi-table operations
  O. Tables without tenant column are skipped
  P. Empty changeset -- no-op sync
  Q. ShadowDB used outside context manager
  R. clone_stats, tables, dialect properties
  S. Engine internals -- dialect detection & type mapping
"""

import sys
from typing import Literal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from safeagentdb import (
    ChangeSet,
    DiffType,
    RowDiff,
    SafeModel,
    ShadowDB,
    SyncError,
)
from safeagentdb.models import _model_registry, get_validator, validate_row
from safeagentdb.engine import (
    _detect_dialect,
    _sqlite_safe_type,
    clone_rows,
    clone_schema_to_sandbox,
    create_sandbox_engine,
    reflect_tables,
)


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture(autouse=True)
def _clear_model_registry():
    """Ensure a clean validator registry for every test."""
    saved = dict(_model_registry)
    _model_registry.clear()
    yield
    _model_registry.clear()
    _model_registry.update(saved)


@pytest.fixture
def prod_engine():
    """Create a fresh production SQLite database for each test."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'todo'
            )
        """))
        conn.execute(text("""
            INSERT INTO tasks (id, user_id, title, status) VALUES
                (1, 42, 'Task A', 'todo'),
                (2, 42, 'Task B', 'in_progress'),
                (3, 99, 'Other user task', 'todo')
        """))
    return engine


@pytest.fixture
def multi_engine():
    """Production DB with multiple tables for multi-table tests."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                email TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE invoices (
                id INTEGER PRIMARY KEY,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL
            )
        """))
        conn.execute(text("""
            CREATE TABLE config (
                id INTEGER PRIMARY KEY,
                key TEXT NOT NULL,
                value TEXT NOT NULL
            )
        """))
        conn.execute(text("INSERT INTO users VALUES (1, 42, 'a@b.com')"))
        conn.execute(text("INSERT INTO users VALUES (2, 99, 'x@y.com')"))
        conn.execute(text("INSERT INTO invoices VALUES (1, 42, 1000, 'pending')"))
        conn.execute(text("INSERT INTO invoices VALUES (2, 99, 500, 'pending')"))
        conn.execute(text("INSERT INTO config VALUES (1, 'theme', 'dark')"))
    return engine


def _register_task_validator():
    class TaskValidator(SafeModel):
        __table_name__ = "tasks"
        id: int
        user_id: int
        title: str
        status: Literal["todo", "in_progress", "done"]
    return TaskValidator


# ============================================================
# A. Imports & public API surface
# ============================================================

class TestImports:
    def test_shadowdb_importable(self):
        assert ShadowDB is not None

    def test_safemodel_importable(self):
        assert SafeModel is not None

    def test_changeset_importable(self):
        assert ChangeSet is not None

    def test_rowdiff_importable(self):
        assert RowDiff is not None

    def test_difftype_importable(self):
        assert DiffType is not None

    def test_syncerror_importable(self):
        assert SyncError is not None

    def test_all_exports(self):
        import safeagentdb
        assert set(safeagentdb.__all__) == {
            "ShadowDB", "SafeModel", "RowDiff", "DiffType", "ChangeSet",
            "SafeAgentDBError", "SchemaError", "SyncError", "ConflictError",
            "MissingValidatorError", "MissingValidatorWarning",
        }


# ============================================================
# B. SafeModel & validator registry
# ============================================================

class TestSafeModel:
    def test_auto_registration(self):
        class Foo(SafeModel):
            __table_name__ = "foo"
            x: int
        assert get_validator("foo") is Foo

    def test_no_registration_without_table_name(self):
        class Bar(SafeModel):
            x: int
        assert get_validator("bar") is None

    def test_strict_mode_rejects_string_for_int(self):
        class Baz(SafeModel):
            __table_name__ = "baz"
            count: int
        with pytest.raises(ValidationError):
            Baz.model_validate({"count": "not_an_int"})

    def test_extra_fields_forbidden(self):
        class Qux(SafeModel):
            __table_name__ = "qux"
            name: str
        with pytest.raises(ValidationError):
            Qux.model_validate({"name": "ok", "hacked": True})

    def test_validate_row_success(self):
        _register_task_validator()
        result = validate_row("tasks", {
            "id": 1, "user_id": 42, "title": "Test", "status": "todo",
        })
        assert result.status == "todo"

    def test_validate_row_bad_data(self):
        _register_task_validator()
        with pytest.raises(ValidationError):
            validate_row("tasks", {
                "id": 1, "user_id": 42, "title": "Test", "status": "nope",
            })

    def test_validate_row_missing_validator(self):
        with pytest.raises(KeyError, match="No SafeModel registered"):
            validate_row("nonexistent_table", {"x": 1})

    def test_get_validator_returns_none(self):
        assert get_validator("no_such_table") is None

    def test_registry_overwrite(self):
        class V1(SafeModel):
            __table_name__ = "dup"
            x: int
        class V2(SafeModel):
            __table_name__ = "dup"
            x: str
        assert get_validator("dup") is V2


# ============================================================
# C. ShadowDB context manager lifecycle
# ============================================================

class TestShadowDBLifecycle:
    def test_enter_creates_sandbox(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            assert sb.sandbox_engine is not None
            assert sb.session is not None

    def test_exit_destroys_sandbox(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            pass
        assert sb.sandbox_engine is None
        assert sb.session is None

    def test_exit_on_exception(self, prod_engine):
        _register_task_validator()
        try:
            with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
                raise ValueError("boom")
        except ValueError:
            pass
        assert sb.sandbox_engine is None


# ============================================================
# D. execute() and query()
# ============================================================

class TestExecuteAndQuery:
    def test_execute_select(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            result = sb.execute("SELECT count(*) FROM tasks")
            assert result.scalar() == 2

    def test_execute_update(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
            row = sb.query("SELECT status FROM tasks WHERE id = 1")
            assert row[0]["status"] == "done"

    def test_query_returns_list_of_dicts(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            rows = sb.query("SELECT * FROM tasks ORDER BY id")
            assert isinstance(rows, list)
            assert isinstance(rows[0], dict)
            assert "id" in rows[0]

    def test_execute_with_params(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            rows = sb.query(
                "SELECT * FROM tasks WHERE status = :s",
                {"s": "todo"},
            )
            assert len(rows) == 1
            assert rows[0]["title"] == "Task A"


# ============================================================
# E. diff() -- INSERT, UPDATE, DELETE detection
# ============================================================

class TestDiff:
    def test_diff_empty_when_no_changes(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            cs = sb.diff()
            assert cs.is_empty

    def test_diff_detects_update(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
            cs = sb.diff()
            assert cs.summary["UPDATE"] == 1

    def test_diff_detects_insert(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute(
                "INSERT INTO tasks (id, user_id, title, status) "
                "VALUES (10, 42, 'New', 'todo')"
            )
            cs = sb.diff()
            assert cs.summary["INSERT"] == 1

    def test_diff_detects_delete(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("DELETE FROM tasks WHERE id = 1")
            cs = sb.diff()
            assert cs.summary["DELETE"] == 1

    def test_diff_mixed_operations(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
            sb.execute("DELETE FROM tasks WHERE id = 2")
            sb.execute(
                "INSERT INTO tasks VALUES (10, 42, 'New', 'todo')"
            )
            cs = sb.diff()
            assert cs.summary == {"INSERT": 1, "UPDATE": 1, "DELETE": 1}


# ============================================================
# F. ChangeSet properties & display methods
# ============================================================

class TestChangeSet:
    def test_is_empty_true(self):
        cs = ChangeSet()
        assert cs.is_empty is True

    def test_is_empty_false(self):
        cs = ChangeSet(diffs=[
            RowDiff(table="t", diff_type=DiffType.INSERT, pk={"id": 1}, new={"id": 1})
        ])
        assert cs.is_empty is False

    def test_summary(self):
        cs = ChangeSet(diffs=[
            RowDiff(table="t", diff_type=DiffType.INSERT, pk={"id": 1}, new={"id": 1}),
            RowDiff(table="t", diff_type=DiffType.UPDATE, pk={"id": 2},
                    old={"id": 2, "x": 1}, new={"id": 2, "x": 2}),
        ])
        assert cs.summary == {"INSERT": 1, "UPDATE": 1, "DELETE": 0}

    def test_is_valid_true_no_validators(self):
        cs = ChangeSet(diffs=[
            RowDiff(table="t", diff_type=DiffType.INSERT, pk={"id": 1}, new={"id": 1}),
        ])
        assert cs.is_valid is True

    def test_is_valid_false_with_validator(self):
        _register_task_validator()
        cs = ChangeSet(diffs=[
            RowDiff(table="tasks", diff_type=DiffType.UPDATE, pk={"id": 1},
                    old={"id": 1, "user_id": 42, "title": "T", "status": "todo"},
                    new={"id": 1, "user_id": 42, "title": "T", "status": "bad"}),
        ])
        assert cs.is_valid is False

    def test_validate_all(self):
        _register_task_validator()
        cs = ChangeSet(diffs=[
            RowDiff(table="tasks", diff_type=DiffType.UPDATE, pk={"id": 1},
                    old={"id": 1, "user_id": 42, "title": "T", "status": "todo"},
                    new={"id": 1, "user_id": 42, "title": "T", "status": "bad"}),
        ])
        results = cs.validate_all()
        assert len(results) == 1
        _, is_valid, msg = results[0]
        assert is_valid is False
        assert "status" in msg

    def test_display_empty(self):
        cs = ChangeSet()
        output = cs.display()
        assert "No changes" in output

    def test_display_returns_string(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
            output = sb.diff().display()
            assert isinstance(output, str)
            assert len(output) > 0

    def test_print_does_not_raise(self, prod_engine, capsys):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
            sb.diff().print()
        captured = capsys.readouterr()
        assert "Row-Level Changes" in captured.out


# ============================================================
# G. RowDiff introspection
# ============================================================

class TestRowDiff:
    def test_changed_columns_on_update(self):
        rd = RowDiff(
            table="t", diff_type=DiffType.UPDATE, pk={"id": 1},
            old={"id": 1, "a": 1, "b": "x"},
            new={"id": 1, "a": 2, "b": "x"},
        )
        assert rd.changed_columns() == ["a"]

    def test_changed_columns_empty_for_insert(self):
        rd = RowDiff(table="t", diff_type=DiffType.INSERT, pk={"id": 1}, new={"id": 1})
        assert rd.changed_columns() == []

    def test_validate_delete_always_ok(self):
        rd = RowDiff(table="t", diff_type=DiffType.DELETE, pk={"id": 1}, old={"id": 1})
        ok, msg = rd.validate()
        assert ok is True

    def test_validate_no_validator_ok(self):
        rd = RowDiff(
            table="unknown", diff_type=DiffType.INSERT, pk={"id": 1}, new={"id": 1},
        )
        ok, msg = rd.validate()
        assert ok is True
        assert "No validator" in msg

    def test_validate_catches_bad_data(self):
        _register_task_validator()
        rd = RowDiff(
            table="tasks", diff_type=DiffType.INSERT, pk={"id": 1},
            new={"id": 1, "user_id": 42, "title": "T", "status": "garbage"},
        )
        ok, msg = rd.validate()
        assert ok is False


# ============================================================
# H. commit_to_production() -- happy path
# ============================================================

class TestCommitHappyPath:
    def test_insert_syncs(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute(
                "INSERT INTO tasks VALUES (10, 42, 'Synced', 'todo')"
            )
            affected = sb.commit_to_production()
            assert affected == 1

        with prod_engine.connect() as conn:
            row = conn.execute(text("SELECT title FROM tasks WHERE id = 10")).fetchone()
            assert row[0] == "Synced"

    def test_update_syncs(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
            sb.commit_to_production()

        with prod_engine.connect() as conn:
            row = conn.execute(text("SELECT status FROM tasks WHERE id = 1")).fetchone()
            assert row[0] == "done"

    def test_delete_syncs(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("DELETE FROM tasks WHERE id = 1")
            sb.commit_to_production()

        with prod_engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM tasks WHERE id = 1")).fetchone()
            assert row is None

    def test_returns_affected_count(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
            sb.execute("DELETE FROM tasks WHERE id = 2")
            sb.execute("INSERT INTO tasks VALUES (10, 42, 'X', 'todo')")
            assert sb.commit_to_production() == 3

    def test_other_tenant_untouched(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("DELETE FROM tasks WHERE id = 1")
            sb.commit_to_production()

        with prod_engine.connect() as conn:
            row = conn.execute(text("SELECT * FROM tasks WHERE id = 3")).fetchone()
            assert row is not None
            assert row[1] == 99


# ============================================================
# I. Pydantic validation gate
# ============================================================

class TestValidationGate:
    def test_bad_status_blocks_sync(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET status = 'yolo' WHERE id = 1")
            with pytest.raises(ValidationError):
                sb.commit_to_production()

    def test_production_untouched_after_rejection(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET status = 'yolo' WHERE id = 1")
            try:
                sb.commit_to_production()
            except ValidationError:
                pass

        with prod_engine.connect() as conn:
            row = conn.execute(text("SELECT status FROM tasks WHERE id = 1")).fetchone()
            assert row[0] == "todo"


# ============================================================
# J. Tenant isolation
# ============================================================

class TestTenantIsolation:
    def test_only_tenant_rows_cloned(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            rows = sb.query("SELECT * FROM tasks")
            assert all(r["user_id"] == 42 for r in rows)
            assert len(rows) == 2

    def test_insert_wrong_tenant_blocked(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("INSERT INTO tasks VALUES (10, 99, 'Evil', 'todo')")
            with pytest.raises(SyncError, match="Tenant breach"):
                sb.commit_to_production()

    def test_update_to_wrong_tenant_blocked(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET user_id = 99 WHERE id = 1")
            with pytest.raises(SyncError, match="Tenant breach"):
                sb.commit_to_production()


# ============================================================
# K. Atomic rollback
# ============================================================

class TestAtomicRollback:
    def test_mixed_valid_and_invalid_rolls_back_all(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET status = 'done' WHERE id = 1")       # valid
            sb.execute("UPDATE tasks SET status = 'garbage' WHERE id = 2")    # invalid
            try:
                sb.commit_to_production()
            except ValidationError:
                pass

        with prod_engine.connect() as conn:
            r1 = conn.execute(text("SELECT status FROM tasks WHERE id = 1")).fetchone()
            r2 = conn.execute(text("SELECT status FROM tasks WHERE id = 2")).fetchone()
            assert r1[0] == "todo"          # NOT 'done' -- rolled back
            assert r2[0] == "in_progress"   # NOT 'garbage' -- rolled back


# ============================================================
# L. Double-commit guard
# ============================================================

class TestDoubleCommit:
    def test_second_commit_raises(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
            sb.commit_to_production()
            with pytest.raises(SyncError, match="already been committed"):
                sb.commit_to_production()


# ============================================================
# M. Non-TTY plain-text fallback
# ============================================================

class TestPlainText:
    def test_plain_text_no_ansi(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
            cs = sb.diff()
            plain = cs._render_plain()
            assert "\x1b[" not in plain
            assert "[SAFE]" in plain
            assert "PASS" in plain

    def test_plain_text_blocked(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            sb.execute("UPDATE tasks SET status = 'bad' WHERE id = 1")
            cs = sb.diff()
            plain = cs._render_plain()
            assert "[BLOCKED]" in plain
            assert "FAIL" in plain

    def test_plain_text_empty(self):
        cs = ChangeSet()
        assert "No changes" in cs._render_plain()


# ============================================================
# N. Multi-table operations
# ============================================================

class TestMultiTable:
    def test_multiple_tables_cloned(self, multi_engine):
        class UV(SafeModel):
            __table_name__ = "users"
            id: int
            user_id: int
            email: str

        class IV(SafeModel):
            __table_name__ = "invoices"
            id: int
            user_id: int
            amount: int
            status: Literal["pending", "paid"]

        with ShadowDB(multi_engine, tables=["users", "invoices"], tenant_id=42) as sb:
            assert sb.clone_stats["users"] == 1
            assert sb.clone_stats["invoices"] == 1

    def test_multi_table_sync(self, multi_engine):
        class UV(SafeModel):
            __table_name__ = "users"
            id: int
            user_id: int
            email: str

        class IV(SafeModel):
            __table_name__ = "invoices"
            id: int
            user_id: int
            amount: int
            status: Literal["pending", "paid"]

        with ShadowDB(multi_engine, tables=["users", "invoices"], tenant_id=42) as sb:
            sb.execute("UPDATE users SET email = 'new@b.com' WHERE id = 1")
            sb.execute("UPDATE invoices SET status = 'paid' WHERE id = 1")
            affected = sb.commit_to_production()
            assert affected == 2


# ============================================================
# O. Tables without tenant column
# ============================================================

class TestNoTenantColumn:
    def test_table_without_tenant_col_skipped(self, multi_engine):
        with ShadowDB(multi_engine, tables=["config"], tenant_id=42) as sb:
            assert sb.clone_stats["config"] == 0
            rows = sb.query("SELECT * FROM config")
            assert len(rows) == 0


# ============================================================
# P. Empty changeset -- no-op sync
# ============================================================

class TestEmptySync:
    def test_no_changes_returns_zero(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            assert sb.commit_to_production() == 0


# ============================================================
# Q. ShadowDB used outside context manager
# ============================================================

class TestOutsideContext:
    def test_execute_outside_raises(self, prod_engine):
        sb = ShadowDB(prod_engine, tables=["tasks"], tenant_id=42)
        with pytest.raises(RuntimeError, match="not open"):
            sb.execute("SELECT 1")

    def test_diff_outside_raises(self, prod_engine):
        sb = ShadowDB(prod_engine, tables=["tasks"], tenant_id=42)
        with pytest.raises(RuntimeError, match="not open"):
            sb.diff()


# ============================================================
# R. clone_stats, tables, dialect properties
# ============================================================

class TestProperties:
    def test_clone_stats(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            assert sb.clone_stats == {"tasks": 2}

    def test_tables_property(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            assert sb.tables == ["tasks"]

    def test_dialect_property(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            assert sb.dialect == "sqlite"

    def test_clone_stats_returns_copy(self, prod_engine):
        _register_task_validator()
        with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
            stats = sb.clone_stats
            stats["tasks"] = 999
            assert sb.clone_stats["tasks"] == 2


# ============================================================
# S. Engine internals
# ============================================================

class TestEngineInternals:
    def test_create_sandbox_engine(self):
        eng = create_sandbox_engine()
        assert eng.dialect.name == "sqlite"
        eng.dispose()

    def test_reflect_tables(self, prod_engine):
        meta = reflect_tables(prod_engine, ["tasks"])
        assert "tasks" in meta.tables

    def test_clone_schema(self, prod_engine):
        meta = reflect_tables(prod_engine, ["tasks"])
        sb_engine = create_sandbox_engine()
        sb_meta = clone_schema_to_sandbox(meta, sb_engine)
        assert "tasks" in sb_meta.tables
        sb_engine.dispose()

    def test_detect_dialect_default(self, prod_engine):
        meta = reflect_tables(prod_engine, ["tasks"])
        assert _detect_dialect(meta) == "sqlite"

    def test_sqlite_safe_type_passthrough(self):
        from sqlalchemy import Integer
        original = Integer()
        result = _sqlite_safe_type(original)
        assert isinstance(result, Integer)
