"""
test_review_findings.py -- The six issues an independent review found in 0.2.0.

Each test reproduces one finding exactly as reported, then pins the behaviour
that replaced it in 0.3.0. Written before the fixes, so each one failed first
against the published 0.2.0 package.

1. A duplicate custom row_key silently overwrote a production row.
2. ON DELETE CASCADE reached across the tenant boundary, unwarned.
3. A populated JSON column crashed the sandbox open with a raw driver error.
4. diff() reported SAFE for a change the commit would reject.
5. The changeset a human reviewed was not necessarily the one committed.
6. Statement order was alphabetical, so a child could be inserted before its
   parent.

Plain pytest, file-based SQLite in tmp_path. The three findings that depend on
server behaviour -- 2, 3 and 6 -- also have PostgreSQL versions in
tests/test_server_backed.py.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from safeagentdb import (
    SafeAgentDBError,
    SafeModel,
    SchemaError,
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
        conn.execute(text("PRAGMA foreign_keys=ON"))
        for statement in ddl:
            conn.execute(text(statement))
    return engine, url


# ============================================================
# 1. A duplicate custom row_key overwrote a production row
# ============================================================


EVENTS_DDL = [
    "CREATE TABLE events (\n"
    " kind TEXT NOT NULL,\n"
    " tenant_id INTEGER NOT NULL,\n"
    " payload TEXT NOT NULL\n"
    ")",
    "INSERT INTO events VALUES ('a', 42, 'original')",
]


def _register_event_validator():
    class EventValidator(SafeModel):
        __table_name__ = "events"
        kind: str
        tenant_id: int
        payload: str

    return EventValidator


class TestDuplicateRowKey:
    def test_a_second_row_sharing_a_key_does_not_overwrite_production(self, tmp_path):
        """The report: diff() showed 1 UPDATE and is_valid True, and the commit
        replaced production's row with the new payload. Two rows went in, one
        row came out, and the one that survived held the wrong data."""
        engine, _ = _prod(tmp_path, "dupe.db", EVENTS_DDL)
        _register_event_validator()

        with ShadowDB(
            engine,
            tables=["events"],
            tenant_id=42,
            tenant_column="tenant_id",
            row_key={"events": ["kind"]},
        ) as sandbox:
            sandbox.execute(
                "INSERT INTO events (kind, tenant_id, payload) VALUES ('a', 42, 'new')"
            )

            # Two sandbox rows now share the key ('a',).
            assert len(sandbox.query("SELECT * FROM events WHERE kind='a'")) == 2

            changeset = sandbox.diff()

            # The changeset must say so rather than merging them into one entry.
            assert changeset.is_valid is False
            assert any("kind" in message for message in changeset.blocking_errors)
            assert any(
                "not unique" in message or "identify" in message
                for message in changeset.blocking_errors
            )
            plain = changeset._render_plain()
            assert "[BLOCKED]" in plain
            assert "events" in plain

            with pytest.raises(SafeAgentDBError):
                sandbox.commit_to_production()

        # Production is exactly as it was.
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT kind, tenant_id, payload FROM events")).fetchall()
        assert rows == [("a", 42, "original")]

    def test_compute_diff_refuses_to_collapse_two_rows_onto_one_key(self):
        """The hard guard, exercised directly: compute_diff must never fold two
        current rows into a single entry, whatever calls it."""
        from safeagentdb.diff import compute_diff

        original = {"events": [{"kind": "a", "tenant_id": 42, "payload": "original"}]}
        current = {
            "events": [
                {"kind": "a", "tenant_id": 42, "payload": "original"},
                {"kind": "a", "tenant_id": 42, "payload": "new"},
            ]
        }

        with pytest.raises(SchemaError, match="not unique|identify"):
            compute_diff(original, current, {"events": ["kind"]})

    def test_an_insert_whose_key_exists_in_production_is_refused(self, tmp_path):
        """Even reached directly, an INSERT onto an existing key must not become
        an overwrite."""
        from safeagentdb.diff import ChangeSet, DiffType, RowDiff
        from safeagentdb.engine import reflect_tables
        from safeagentdb.sync import apply_changeset

        engine, _ = _prod(tmp_path, "dupe_insert.db", EVENTS_DDL)
        _register_event_validator()
        meta = reflect_tables(engine, ["events"])

        changeset = ChangeSet(
            diffs=[
                RowDiff(
                    table="events",
                    diff_type=DiffType.INSERT,
                    pk={"kind": "a"},
                    new={"kind": "a", "tenant_id": 42, "payload": "new"},
                )
            ]
        )

        with pytest.raises(SyncError, match="already exists"):
            apply_changeset(
                engine,
                meta,
                changeset,
                "tenant_id",
                42,
                row_keys={"events": ["kind"]},
            )

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT payload FROM events")).fetchall()
        assert rows == [("original",)]


# ============================================================
# 2. ON DELETE CASCADE crossed the tenant boundary
# ============================================================


CASCADE_DDL = [
    "CREATE TABLE parent (\n"
    " id INTEGER PRIMARY KEY,\n"
    " tenant_id INTEGER NOT NULL,\n"
    " label TEXT NOT NULL\n"
    ")",
    "CREATE TABLE child (\n"
    " id INTEGER PRIMARY KEY,\n"
    " tenant_id INTEGER NOT NULL,\n"
    " parent_id INTEGER NOT NULL REFERENCES parent(id) ON DELETE CASCADE,\n"
    " note TEXT NOT NULL\n"
    ")",
    "INSERT INTO parent VALUES (1, 42, 'owned by 42')",
    "INSERT INTO child VALUES (10, 99, 1, 'belongs to 99')",
]


def _register_parent_validator():
    class ParentValidator(SafeModel):
        __table_name__ = "parent"
        id: int
        tenant_id: int
        label: str

    return ParentValidator


class TestCascadeCrossesTenants:
    def test_a_cascading_reference_is_reported_at_open(self, tmp_path):
        """The report: unsupported_constraints was empty and nothing warned,
        even though deleting the parent would reach another tenant's row."""
        engine, _ = _prod(tmp_path, "cascade_report.db", CASCADE_DDL)
        _register_parent_validator()

        with ShadowDB(
            engine, tables=["parent"], tenant_id=42, tenant_column="tenant_id"
        ) as sandbox:
            reported = sandbox.unsupported_constraints
            assert any("child" in item for item in reported), reported
            assert any("CASCADE" in item.upper() for item in reported), reported

            sandbox.execute("UPDATE parent SET label='renamed' WHERE id=1")
            plain = sandbox.diff()._render_plain()
            assert "[NOT ENFORCED IN SANDBOX]" in plain
            assert "child" in plain

    def test_a_delete_that_would_cascade_across_tenants_is_refused(self, tmp_path):
        """The report: the delete went through and took tenant 99's child with
        it. Now the whole changeset is refused."""
        engine, _ = _prod(tmp_path, "cascade_delete.db", CASCADE_DDL)
        _register_parent_validator()

        with ShadowDB(
            engine, tables=["parent"], tenant_id=42, tenant_column="tenant_id"
        ) as sandbox:
            sandbox.execute("DELETE FROM parent WHERE id=1")

            with pytest.raises(SafeAgentDBError) as excinfo:
                sandbox.commit_to_production()

            message = str(excinfo.value)
            assert "child" in message
            assert "99" in message

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM parent")).scalar_one() == 1
            assert conn.execute(text("SELECT COUNT(*) FROM child")).scalar_one() == 1

    def test_a_delete_whose_cascade_stays_inside_the_tenant_is_allowed(self, tmp_path):
        """The guard must not refuse a cascade confined to the acting tenant."""
        engine, _ = _prod(
            tmp_path,
            "cascade_same.db",
            [
                CASCADE_DDL[0],
                CASCADE_DDL[1],
                "INSERT INTO parent VALUES (1, 42, 'owned by 42')",
                "INSERT INTO child VALUES (10, 42, 1, 'also 42')",
            ],
        )
        _register_parent_validator()

        with ShadowDB(
            engine, tables=["parent"], tenant_id=42, tenant_column="tenant_id"
        ) as sandbox:
            sandbox.execute("DELETE FROM parent WHERE id=1")
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM parent")).scalar_one() == 0
            # SQLite's own cascade removed the child, which is correct here.
            assert conn.execute(text("SELECT COUNT(*) FROM child")).scalar_one() == 0


# ============================================================
# 3. A populated JSON column crashed the sandbox open
# ============================================================


class TestJsonColumns:
    @staticmethod
    def _json_engine(tmp_path, name, values):
        """A production table with a JSON column, holding the given values."""
        engine, _ = _prod(
            tmp_path,
            name,
            [
                "CREATE TABLE docs (\n"
                " id INTEGER PRIMARY KEY,\n"
                " tenant_id INTEGER NOT NULL,\n"
                " title TEXT NOT NULL,\n"
                " data JSON\n"
                ")",
            ],
        )
        with engine.begin() as conn:
            for index, value in enumerate(values, start=1):
                conn.execute(
                    text("INSERT INTO docs VALUES (:i, 42, :t, :d)"),
                    {
                        "i": index,
                        "t": f"doc {index}",
                        "d": None if value is None else json.dumps(value),
                    },
                )
        return engine

    @staticmethod
    def _register():
        class DocValidator(SafeModel):
            __table_name__ = "docs"
            id: int
            tenant_id: int
            title: str
            data: dict | list | None

        return DocValidator

    @pytest.mark.parametrize(
        "value",
        [
            {"a": 1},
            [1, 2, 3],
            {"outer": {"inner": [1, {"deep": True}]}},
            None,
        ],
        ids=["dict", "list", "nested", "null"],
    )
    def test_the_sandbox_opens_with_a_populated_json_column(self, tmp_path, value):
        """The report: __enter__ raised sqlite3.ProgrammingError: type 'dict' is
        not supported."""
        engine = self._json_engine(tmp_path, f"json_open_{id(value)}.db", [value])
        self._register()

        with ShadowDB(
            engine, tables=["docs"], tenant_id=42, tenant_column="tenant_id"
        ) as sandbox:
            rows = sandbox.query("SELECT id, title FROM docs")
            assert rows == [{"id": 1, "title": "doc 1"}]

    def test_an_untouched_json_value_is_not_reported_as_changed(self, tmp_path):
        engine = self._json_engine(tmp_path, "json_untouched.db", [{"a": 1}])
        self._register()

        with ShadowDB(
            engine, tables=["docs"], tenant_id=42, tenant_column="tenant_id"
        ) as sandbox:
            sandbox.execute("UPDATE docs SET title='renamed' WHERE id=1")
            changeset = sandbox.diff()
            (diff,) = changeset.diffs
            assert diff.changed_columns() == ["title"]
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            title, data = conn.execute(
                text("SELECT title, data FROM docs WHERE id=1")
            ).one()
        assert title == "renamed"
        assert json.loads(data) == {"a": 1}

    def test_a_changed_json_value_round_trips(self, tmp_path):
        engine = self._json_engine(tmp_path, "json_changed.db", [{"a": 1}])
        self._register()

        with ShadowDB(
            engine, tables=["docs"], tenant_id=42, tenant_column="tenant_id"
        ) as sandbox:
            sandbox.execute(
                """UPDATE docs SET data='{"a": 2, "b": [1, 2]}' WHERE id=1"""
            )
            assert sandbox.commit_to_production() == 1

        with engine.connect() as conn:
            data = conn.execute(text("SELECT data FROM docs WHERE id=1")).scalar_one()
        assert json.loads(data) == {"a": 2, "b": [1, 2]}

    def test_a_driver_error_at_open_is_wrapped(self, tmp_path):
        """Whatever goes wrong while opening the sandbox, a raw driver exception
        must not reach the caller."""
        import sqlalchemy

        engine, _ = _prod(
            tmp_path,
            "open_boom.db",
            [
                "CREATE TABLE docs (\n"
                " id INTEGER PRIMARY KEY,\n"
                " tenant_id INTEGER NOT NULL\n"
                ")",
                "INSERT INTO docs VALUES (1, 42)",
            ],
        )
        self._register()

        shadow = ShadowDB(
            engine, tables=["docs"], tenant_id=42, tenant_column="tenant_id"
        )

        original = shadow.__class__._take_snapshot

        def boom(self):
            raise sqlalchemy.exc.OperationalError("SELECT 1", {}, Exception("disk I/O error"))

        shadow.__class__._take_snapshot = boom
        try:
            with pytest.raises(SafeAgentDBError):
                shadow.__enter__()
        finally:
            shadow.__class__._take_snapshot = original


# ============================================================
# 4. diff() said SAFE for a change the commit would reject
# ============================================================


TASKS_DDL = [
    "CREATE TABLE tasks (\n"
    " id INTEGER PRIMARY KEY,\n"
    " tenant_id INTEGER NOT NULL,\n"
    " title TEXT NOT NULL,\n"
    " status TEXT NOT NULL\n"
    ")",
    "INSERT INTO tasks VALUES (1, 42, 'Ship v2', 'todo')",
    "INSERT INTO tasks VALUES (2, 99, 'Theirs', 'todo')",
]


def _register_task_validator():
    class TaskValidator(SafeModel):
        __table_name__ = "tasks"
        id: int
        tenant_id: int
        title: str
        status: str

    return TaskValidator


class TestDiffAgreesWithCommit:
    def test_a_tenant_breach_is_visible_in_the_diff(self, tmp_path):
        """The report: diff().is_valid was True and the banner said SAFE TO
        COMMIT, then the commit raised SyncError for a tenant breach."""
        engine, _ = _prod(tmp_path, "breach.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, tenant_column="tenant_id"
        ) as sandbox:
            sandbox.execute("UPDATE tasks SET tenant_id=99 WHERE id=1")

            changeset = sandbox.diff()
            assert changeset.is_valid is False
            plain = changeset._render_plain()
            assert "[BLOCKED]" in plain
            assert "SAFE TO COMMIT" not in plain
            assert any("tenant" in m.lower() for m in changeset.blocking_errors)

            with pytest.raises(SyncError, match="[Tt]enant breach"):
                sandbox.commit_to_production()

    def test_the_banner_still_warns_that_commit_can_fail(self, tmp_path):
        """Drift and cross-tenant uniqueness cannot be checked without
        production, so a clean diff must not promise the commit will succeed."""
        engine, _ = _prod(tmp_path, "banner.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, tenant_column="tenant_id"
        ) as sandbox:
            sandbox.execute("UPDATE tasks SET status='done' WHERE id=1")
            changeset = sandbox.diff()
            assert changeset.is_valid is True
            plain = changeset._render_plain()
            assert "production" in plain.lower()

    def test_a_generated_key_problem_is_still_visible(self, tmp_path):
        """The checks that already ran in diff() must keep doing so."""
        engine, _ = _prod(tmp_path, "still.db", TASKS_DDL)
        # No validator registered at all.
        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, tenant_column="tenant_id"
        ) as sandbox:
            sandbox.execute("UPDATE tasks SET status='done' WHERE id=1")
            assert sandbox.diff().is_valid is False


# ============================================================
# 5. The reviewed changeset was not the committed changeset
# ============================================================


class TestReviewedChangeset:
    def test_committing_a_stale_changeset_is_refused(self, tmp_path):
        """The report: diff() showed 1 UPDATE, another statement ran, and the
        commit wrote 2 changes -- more than was reviewed."""
        engine, _ = _prod(tmp_path, "stale.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, tenant_column="tenant_id"
        ) as sandbox:
            sandbox.execute("UPDATE tasks SET status='done' WHERE id=1")
            reviewed = sandbox.diff()
            assert reviewed.summary == {"INSERT": 0, "UPDATE": 1, "DELETE": 0}

            # Something else happens after the review.
            sandbox.execute("DELETE FROM tasks WHERE id=1")

            with pytest.raises(SyncError, match="changed since"):
                sandbox.commit_to_production(changeset=reviewed)

        with engine.connect() as conn:
            rows = conn.execute(text("SELECT id, status FROM tasks ORDER BY id")).fetchall()
        assert rows == [(1, "todo"), (2, "todo")]

    def test_committing_the_reviewed_changeset_works(self, tmp_path):
        engine, _ = _prod(tmp_path, "fresh.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, tenant_column="tenant_id"
        ) as sandbox:
            sandbox.execute("UPDATE tasks SET status='done' WHERE id=1")
            reviewed = sandbox.diff()
            assert sandbox.commit_to_production(changeset=reviewed) == 1

        with engine.connect() as conn:
            status = conn.execute(text("SELECT status FROM tasks WHERE id=1")).scalar_one()
        assert status == "done"

    def test_calling_without_a_changeset_still_works(self, tmp_path):
        engine, _ = _prod(tmp_path, "noarg.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, tenant_column="tenant_id"
        ) as sandbox:
            sandbox.execute("UPDATE tasks SET status='done' WHERE id=1")
            assert sandbox.commit_to_production() == 1

    def test_the_fingerprint_is_stable_and_content_addressed(self, tmp_path):
        engine, _ = _prod(tmp_path, "fp.db", TASKS_DDL)
        _register_task_validator()

        with ShadowDB(
            engine, tables=["tasks"], tenant_id=42, tenant_column="tenant_id"
        ) as sandbox:
            sandbox.execute("UPDATE tasks SET status='done' WHERE id=1")
            first = sandbox.diff()
            second = sandbox.diff()
            assert first.fingerprint == second.fingerprint

            sandbox.execute("UPDATE tasks SET title='renamed' WHERE id=1")
            third = sandbox.diff()
            assert third.fingerprint != first.fingerprint


# ============================================================
# 6. Statement order ignored foreign-key dependencies
# ============================================================


FK_ORDER_DDL = [
    "CREATE TABLE b_parent (\n"
    " id INTEGER PRIMARY KEY,\n"
    " tenant_id INTEGER NOT NULL,\n"
    " label TEXT NOT NULL\n"
    ")",
    "CREATE TABLE a_child (\n"
    " id INTEGER PRIMARY KEY,\n"
    " tenant_id INTEGER NOT NULL,\n"
    " parent_id INTEGER NOT NULL REFERENCES b_parent(id),\n"
    " note TEXT NOT NULL\n"
    ")",
]


def _register_fk_validators():
    class ParentValidator(SafeModel):
        __table_name__ = "b_parent"
        id: int
        tenant_id: int
        label: str

    class ChildValidator(SafeModel):
        __table_name__ = "a_child"
        id: int
        tenant_id: int
        parent_id: int
        note: str

    return ParentValidator, ChildValidator


class TestStatementOrder:
    def test_a_parent_is_inserted_before_its_child(self, tmp_path):
        """The report: alphabetical ordering put a_child first and production
        rejected it."""
        engine, _ = _prod(tmp_path, "fk_order.db", FK_ORDER_DDL)
        _register_fk_validators()

        with ShadowDB(
            engine,
            tables=["a_child", "b_parent"],
            tenant_id=42,
            tenant_column="tenant_id",
        ) as sandbox:
            sandbox.execute(
                "INSERT INTO b_parent (id, tenant_id, label) VALUES (1, 42, 'parent')"
            )
            sandbox.execute(
                "INSERT INTO a_child (id, tenant_id, parent_id, note) "
                "VALUES (10, 42, 1, 'child')"
            )
            assert sandbox.commit_to_production() == 2

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM b_parent")).scalar_one() == 1
            assert conn.execute(text("SELECT COUNT(*) FROM a_child")).scalar_one() == 1

    def test_a_child_is_deleted_before_its_parent(self, tmp_path):
        engine, _ = _prod(
            tmp_path,
            "fk_delete.db",
            FK_ORDER_DDL
            + [
                "INSERT INTO b_parent VALUES (1, 42, 'parent')",
                "INSERT INTO a_child VALUES (10, 42, 1, 'child')",
            ],
        )
        _register_fk_validators()

        with ShadowDB(
            engine,
            tables=["a_child", "b_parent"],
            tenant_id=42,
            tenant_column="tenant_id",
        ) as sandbox:
            sandbox.execute("DELETE FROM a_child WHERE id=10")
            sandbox.execute("DELETE FROM b_parent WHERE id=1")
            assert sandbox.commit_to_production() == 2

        with engine.connect() as conn:
            assert conn.execute(text("SELECT COUNT(*) FROM b_parent")).scalar_one() == 0
            assert conn.execute(text("SELECT COUNT(*) FROM a_child")).scalar_one() == 0

    def test_a_dependency_cycle_is_refused(self, tmp_path):
        """A cycle has no safe order, so it must be refused rather than tried."""
        from safeagentdb.sync import table_write_order

        with pytest.raises(SchemaError, match="cycle"):
            table_write_order({"a": {"b"}, "b": {"a"}})

    def test_the_integrity_error_does_not_invent_a_cause(self, tmp_path):
        """IntegrityViolationError used to blame the tenant-scoped clone for
        every rejection, including ones it could not have caused."""
        engine, _ = _prod(
            tmp_path,
            "fk_message.db",
            FK_ORDER_DDL + ["INSERT INTO b_parent VALUES (1, 42, 'parent')"],
        )
        _register_fk_validators()

        from safeagentdb import IntegrityViolationError

        with ShadowDB(
            engine,
            tables=["a_child", "b_parent"],
            tenant_id=42,
            tenant_column="tenant_id",
        ) as sandbox:
            # References a parent that does not exist anywhere.
            sandbox.execute(
                "INSERT INTO a_child (id, tenant_id, parent_id, note) "
                "VALUES (10, 42, 999, 'orphan')"
            )
            with pytest.raises(IntegrityViolationError) as excinfo:
                sandbox.commit_to_production()

        message = str(excinfo.value)
        assert "a_child" in message
        assert "only this tenant's rows" not in message
