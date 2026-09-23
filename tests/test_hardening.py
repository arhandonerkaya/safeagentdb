"""
test_hardening.py -- Tests for the machinery added in 0.2.0.

Where test_weaknesses.py proves the four audit findings stay fixed, this file
covers the parts built to fix them: the row-key resolver, the schema
translation layer, the unsupported-constraint report, the conflict options and
the validator policy.

Plain pytest, file-based SQLite in tmp_path, postgresql-typed MetaData built in
memory. No server, no network, no Docker.
"""

from __future__ import annotations

import warnings
from typing import Literal

import pytest
from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID

from safeagentdb import (
    ConflictError,
    ConflictWarning,
    MissingValidatorWarning,
    SafeModel,
    SchemaError,
    ShadowDB,
)
from safeagentdb.engine import (
    _is_balanced,
    _sqlite_safe_server_default,
    clone_schema_to_sandbox,
    create_sandbox_engine,
    reflect_tables,
)
from safeagentdb.models import _model_registry


@pytest.fixture(autouse=True)
def _isolated_registry():
    saved = dict(_model_registry)
    _model_registry.clear()
    yield
    _model_registry.clear()
    _model_registry.update(saved)


def _prod_engine(tmp_path, name: str, ddl: list[str]):
    url = f"sqlite:///{tmp_path / name}"
    engine = create_engine(url)
    with engine.begin() as conn:
        for stmt in ddl:
            conn.execute(text(stmt))
    return engine, url


TASKS_DDL = [
    "CREATE TABLE tasks (\n"
    " id INTEGER PRIMARY KEY,\n"
    " user_id INTEGER NOT NULL,\n"
    " title TEXT NOT NULL,\n"
    " status TEXT NOT NULL DEFAULT 'todo'\n"
    ")",
    "INSERT INTO tasks VALUES (1, 42, 'Ship v2', 'todo')",
    "INSERT INTO tasks VALUES (2, 42, 'Write docs', 'todo')",
]


def _register_task_validator():
    class TaskValidator(SafeModel):
        __table_name__ = "tasks"
        id: int
        user_id: int
        title: str
        status: str

    return TaskValidator


# ============================================================
# Row key resolution
# ============================================================


class TestRowKeyResolution:
    def test_primary_key_is_used_by_default(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "pk.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            assert sandbox.row_keys == {"tasks": ["id"]}

    def test_explicit_row_key_overrides_the_primary_key(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "override.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, row_key={"tasks": ["title"]}
        ) as sandbox:
            assert sandbox.row_keys == {"tasks": ["title"]}

            sandbox.execute("UPDATE tasks SET status='done' WHERE title='Ship v2'")
            (diff,) = sandbox.diff().diffs
            assert diff.pk == {"title": "Ship v2"}
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, status FROM tasks ORDER BY id")).fetchall()
        assert rows == [(1, "done"), (2, "todo")]

    def test_row_keys_property_returns_a_copy(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "copy.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.row_keys["tasks"].append("tampered")
            assert sandbox.row_keys == {"tasks": ["id"]}

    def test_composite_row_key_is_accepted(self, tmp_path):
        engine, _ = _prod_engine(
            tmp_path,
            "composite.db",
            [
                "CREATE TABLE memberships (\n"
                " user_id INTEGER NOT NULL,\n"
                " team_id INTEGER NOT NULL,\n"
                " role TEXT NOT NULL\n"
                ")",
                "INSERT INTO memberships VALUES (42,1,'member'),(42,2,'admin')",
            ],
        )

        class MembershipValidator(SafeModel):
            __table_name__ = "memberships"
            user_id: int
            team_id: int
            role: str

        with ShadowDB(
            engine,
            tables=["memberships"],
            tenant_id=42,
            row_key={"memberships": ["user_id", "team_id"]},
        ) as sandbox:
            sandbox.execute("UPDATE memberships SET role='owner' WHERE team_id=1")
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT team_id, role FROM memberships ORDER BY team_id")
            ).fetchall()
        assert rows == [(1, "owner"), (2, "admin")]

    def test_sandbox_engine_is_disposed_when_enter_fails(self, tmp_path):
        engine, _ = _prod_engine(
            tmp_path,
            "dispose.db",
            [
                "CREATE TABLE events (user_id INTEGER NOT NULL, kind TEXT NOT NULL)",
                "INSERT INTO events VALUES (42, 'login')",
            ],
        )
        shadow = ShadowDB(engine, tables=["events"], tenant_id=42)

        with pytest.raises(SchemaError):
            shadow.__enter__()

        assert shadow.sandbox_engine is None
        assert shadow.session is None


# ============================================================
# Schema translation
# ============================================================


class TestServerDefaultTranslation:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("'free'::character varying", "'free'"),
            ("0::numeric(10,2)", "0"),
            ("now()", "CURRENT_TIMESTAMP"),
            ("CURRENT_TIMESTAMP", "CURRENT_TIMESTAMP"),
            ("true", "1"),
            ("false", "0"),
            ("'{}'", "'{}'"),
        ],
    )
    def test_translatable_defaults(self, raw, expected):
        translated, reason = _sqlite_safe_server_default(raw)
        assert translated == expected
        assert reason is None

    @pytest.mark.parametrize(
        "raw",
        [
            "nextval('users_id_seq'::regclass)",
            "gen_random_uuid()",
            "uuid_generate_v4()",
        ],
    )
    def test_untranslatable_defaults_are_reported(self, raw):
        translated, reason = _sqlite_safe_server_default(raw)
        assert translated is None
        assert "no SQLite equivalent" in reason

    def test_pg_cast_default_survives_the_clone(self):
        meta = MetaData()
        Table(
            "users",
            meta,
            Column("id", Integer, primary_key=True),
            Column("user_id", Integer, nullable=False),
            Column(
                "plan",
                String(32),
                nullable=False,
                server_default=text("'free'::character varying"),
            ),
            Column("prefs", JSONB),
        )

        sandbox = create_sandbox_engine()
        try:
            clone_schema_to_sandbox(meta, sandbox)
            with sandbox.connect() as conn:
                conn.execute(
                    text("INSERT INTO users (id, user_id) VALUES (1, 42)")
                )
                conn.commit()
                plan = conn.execute(text("SELECT plan FROM users")).scalar_one()
            assert plan == "free"
        finally:
            sandbox.dispose()

    def test_sequence_default_is_dropped_and_recorded(self):
        meta = MetaData()
        Table(
            "users",
            meta,
            Column("id", Integer, primary_key=True),
            Column("user_id", Integer, nullable=False),
            Column("code", String(16), server_default=text("gen_random_uuid()")),
            Column("prefs", JSONB),
        )

        sandbox = create_sandbox_engine()
        try:
            sb_meta = clone_schema_to_sandbox(meta, sandbox)
            reported = sb_meta.info["unsupported_constraints"]
            assert any("gen_random_uuid" in item for item in reported)
            assert sb_meta.tables["users"].c.code.server_default is None
        finally:
            sandbox.dispose()


class TestCheckExpressionBalance:
    @pytest.mark.parametrize(
        "expression,balanced",
        [
            ("age >= 0", True),
            ("age >= 0)", False),
            ("(a > 1 AND b < 2)", True),
            ("(a > 1", False),
            ("status <> ')'", True),
            ("name <> 'O''Brien'", True),
            ("name = 'unterminated", False),
        ],
    )
    def test_balance_detection(self, expression, balanced):
        assert _is_balanced(expression) is balanced


class TestForeignKeyHandling:
    def test_foreign_key_to_an_uncloned_table_is_dropped_and_recorded(self):
        meta = MetaData()
        Table(
            "tasks",
            meta,
            Column("id", Integer, primary_key=True),
            Column("user_id", Integer, nullable=False),
            Column("ext_id", Integer, ForeignKey("external_system.id")),
            Column("prefs", JSONB),
        )

        sandbox = create_sandbox_engine()
        try:
            sb_meta = clone_schema_to_sandbox(meta, sandbox)
            reported = sb_meta.info["unsupported_constraints"]
            assert any("external_system" in item for item in reported)
            # Sorting used to raise NoReferencedTableError on metadata like this.
            assert [t.name for t in sb_meta.sorted_tables] == ["tasks"]
        finally:
            sandbox.dispose()

    def test_partial_clone_loads_despite_dangling_parents(self, tmp_path):
        """A tenant-scoped clone is a partial view, so a row may reference a
        parent that was not copied. Loading must not fail, but the agent's own
        writes must still be foreign-key checked."""
        engine, _ = _prod_engine(
            tmp_path,
            "partial.db",
            [
                "CREATE TABLE users (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
                "CREATE TABLE tasks (\n"
                " id INTEGER PRIMARY KEY,\n"
                " user_id INTEGER NOT NULL,\n"
                " owner_id INTEGER NOT NULL REFERENCES users(id),\n"
                " title TEXT NOT NULL\n"
                ")",
                "INSERT INTO users VALUES (1, 42), (2, 99)",
                # tenant 42's row points at tenant 99's user, which is not cloned
                "INSERT INTO tasks VALUES (10, 42, 2, 'cross-parent')",
            ],
        )

        class UserValidator(SafeModel):
            __table_name__ = "users"
            id: int
            user_id: int

        class TaskValidator(SafeModel):
            __table_name__ = "tasks"
            id: int
            user_id: int
            owner_id: int
            title: str

        with ShadowDB(engine, tables=["users", "tasks"], tenant_id=42) as sandbox:
            assert sandbox.clone_stats == {"users": 1, "tasks": 1}

            with sandbox.sandbox_engine.connect() as conn:
                assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1

            from sqlalchemy.exc import IntegrityError

            with pytest.raises(IntegrityError):
                sandbox.execute(
                    "INSERT INTO tasks (id,user_id,owner_id,title) "
                    "VALUES (99,42,777,'orphan')"
                )
                sandbox.session.commit()


class TestUnsupportedConstraintReport:
    def test_dialect_type_mapping_is_reported(self):
        meta = MetaData()
        Table(
            "users",
            meta,
            Column("id", Integer, primary_key=True),
            Column("user_id", Integer, nullable=False),
            Column("prefs", JSONB),
            Column("external_id", UUID),
        )

        sandbox = create_sandbox_engine()
        try:
            sb_meta = clone_schema_to_sandbox(meta, sandbox)
            reported = sb_meta.info["unsupported_constraints"]
            assert any("users.prefs" in item and "JSONB" in item for item in reported)
            assert any("users.external_id" in item and "UUID" in item for item in reported)
        finally:
            sandbox.dispose()

    def test_report_is_empty_for_a_plain_sqlite_schema(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "plain.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            assert sandbox.unsupported_constraints == []
            assert "NOT ENFORCED" not in sandbox.diff()._render_plain()

    def test_report_appears_in_the_plain_diff_output(self, tmp_path):
        engine, _ = _prod_engine(
            tmp_path,
            "reported.db",
            [
                # one-line CHECK: reflection returns an unbalanced expression
                "CREATE TABLE tasks ("
                " id INTEGER PRIMARY KEY,"
                " user_id INTEGER NOT NULL,"
                " title TEXT NOT NULL,"
                " status TEXT NOT NULL,"
                " priority INTEGER CHECK (priority >= 0))",
                "INSERT INTO tasks VALUES (1, 42, 'Ship v2', 'todo', 1)",
            ],
        )

        class TaskValidator(SafeModel):
            __table_name__ = "tasks"
            id: int
            user_id: int
            title: str
            status: str
            priority: int

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            assert sandbox.unsupported_constraints

            sandbox.execute("UPDATE tasks SET status='done' WHERE id=1")
            plain = sandbox.diff()._render_plain()
            assert "[NOT ENFORCED IN SANDBOX]" in plain
            assert "CHECK" in plain

    def test_report_appears_even_with_no_changes(self, tmp_path):
        engine, _ = _prod_engine(
            tmp_path,
            "reported_empty.db",
            [
                "CREATE TABLE tasks ("
                " id INTEGER PRIMARY KEY,"
                " user_id INTEGER NOT NULL,"
                " priority INTEGER CHECK (priority >= 0))",
                "INSERT INTO tasks VALUES (1, 42, 1)",
            ],
        )

        class TaskValidator(SafeModel):
            __table_name__ = "tasks"
            id: int
            user_id: int
            priority: int

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            changeset = sandbox.diff()
            assert changeset.is_empty
            plain = changeset._render_plain()
            assert "No changes detected" in plain
            assert "[NOT ENFORCED IN SANDBOX]" in plain

    def test_unsupported_constraints_property_returns_a_copy(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "copy2.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.unsupported_constraints.append("tampered")
            assert sandbox.unsupported_constraints == []


# ============================================================
# Conflict handling options
# ============================================================


class TestConflictOptions:
    def test_invalid_on_conflict_is_rejected_up_front(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "badopt.db", TASKS_DDL)
        with pytest.raises(ValueError, match="on_conflict"):
            ShadowDB(engine, tables=["tasks"], tenant_id=42, on_conflict="explode")

    def test_conflict_error_is_a_sync_error(self):
        error = ConflictError("boom", table="t", row_key={"id": 1}, columns=["a"])
        from safeagentdb import SyncError

        assert isinstance(error, SyncError)
        assert error.table == "t"
        assert error.row_key == {"id": 1}
        assert error.columns == ["a"]

    def test_delete_of_an_already_deleted_row_conflicts(self, tmp_path):
        engine, url = _prod_engine(tmp_path, "dbldelete.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("DELETE FROM tasks WHERE id=1")

            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(text("DELETE FROM tasks WHERE id=1"))

            with pytest.raises(ConflictError, match="deleted in production"):
                sandbox.commit_to_production()

    def test_ignore_lets_an_already_deleted_row_pass(self, tmp_path):
        engine, url = _prod_engine(tmp_path, "dbldelete_ign.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, on_conflict="ignore"
        ) as sandbox:
            sandbox.execute("DELETE FROM tasks WHERE id=1")
            sandbox.execute("UPDATE tasks SET status='done' WHERE id=2")

            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(text("DELETE FROM tasks WHERE id=1"))

            with pytest.warns(ConflictWarning):
                assert sandbox.commit_to_production() == 1
            assert [s.row_key for s in sandbox.skipped_conflicts] == [{"id": 1}]

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, status FROM tasks")).fetchall()
        assert rows == [(2, "done")]

    def test_a_concurrent_insert_does_not_block_an_unrelated_update(self, tmp_path):
        engine, url = _prod_engine(tmp_path, "unrelated.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET status='done' WHERE id=1")

            other = create_engine(url)
            with other.begin() as conn:
                conn.execute(
                    text("INSERT INTO tasks VALUES (3, 42, 'Added later', 'todo')")
                )

            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, status FROM tasks ORDER BY id")).fetchall()
        assert rows == [(1, "done"), (2, "todo"), (3, "todo")]


# ============================================================
# Validator policy
# ============================================================


class TestValidatorPolicy:
    def test_require_validators_false_warns_once_per_row(self, tmp_path):
        engine, _ = _prod_engine(tmp_path, "warnrows.db", TASKS_DDL)

        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, require_validators=False
        ) as sandbox:
            sandbox.execute("UPDATE tasks SET status='done' WHERE id IN (1,2)")
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                assert sandbox.commit_to_production() == 2
            messages = [
                str(w.message) for w in caught
                if issubclass(w.category, MissingValidatorWarning)
            ]
            assert len(messages) == 2
            assert all("No SafeModel registered for table 'tasks'" in m for m in messages)

    def test_a_registered_validator_still_blocks_bad_data(self, tmp_path):
        from pydantic import ValidationError

        engine, _ = _prod_engine(tmp_path, "stillblocks.db", TASKS_DDL)

        class StrictTaskValidator(SafeModel):
            __table_name__ = "tasks"
            id: int
            user_id: int
            title: str
            status: Literal["todo", "in_progress", "done"]

        with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
            sandbox.execute("UPDATE tasks SET status='yolo' WHERE id=1")
            assert sandbox.diff().is_valid is False
            with pytest.raises(ValidationError):
                sandbox.commit_to_production()

        with engine.connect() as conn:
            assert (
                conn.execute(text("SELECT status FROM tasks WHERE id=1")).scalar_one()
                == "todo"
            )


# ============================================================
# Reflection still behaves
# ============================================================


class TestReflectionIntegration:
    def test_reflect_pulls_in_foreign_key_targets(self, tmp_path):
        engine, _ = _prod_engine(
            tmp_path,
            "resolve.db",
            [
                "CREATE TABLE users (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL)",
                "CREATE TABLE tasks (\n"
                " id INTEGER PRIMARY KEY,\n"
                " user_id INTEGER NOT NULL,\n"
                " owner_id INTEGER NOT NULL REFERENCES users(id)\n"
                ")",
            ],
        )
        meta = reflect_tables(engine, ["tasks"])
        # resolve_fks is on by default, so 'users' comes along for the ride.
        assert sorted(meta.tables) == ["tasks", "users"]
