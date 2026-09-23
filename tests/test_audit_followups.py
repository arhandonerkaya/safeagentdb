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

from safeagentdb import SafeModel, ShadowDB, SyncError
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
