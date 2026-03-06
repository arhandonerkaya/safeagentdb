"""
sandbox.py - The main developer-facing API.

Usage:
    with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sandbox:
        sandbox.execute("UPDATE tasks SET status='done' WHERE id=1")
        print(sandbox.diff().display())
        sandbox.commit_to_production()
"""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import CursorResult, MetaData, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from safeagentdb.diff import ChangeSet, compute_diff
from safeagentdb.engine import (
    clone_rows,
    clone_schema_to_sandbox,
    create_sandbox_engine,
    reflect_tables,
)
from safeagentdb.sync import SyncError, apply_changeset


class ShadowDB:
    """Shadow-Sandbox database layer.

    Clones tenant-scoped data into an in-memory SQLite sandbox,
    lets AI operate freely, then syncs approved changes back to
    production in a single atomic transaction.

    Works with any SQLAlchemy-supported production database
    (PostgreSQL, MySQL, SQLite, etc.). The sandbox is always
    in-memory SQLite for speed and isolation.

    Args:
        prod_engine: SQLAlchemy Engine connected to the production database.
        tables: List of table names to clone into the sandbox.
        tenant_id: The tenant/user ID to scope all operations to.
        tenant_column: Column name used for tenant isolation (default: "user_id").
    """

    def __init__(
        self,
        prod_engine: Engine,
        tables: Sequence[str],
        tenant_id: Any,
        tenant_column: str = "user_id",
    ) -> None:
        self.prod_engine = prod_engine
        self.table_names = list(tables)
        self.tenant_id = tenant_id
        self.tenant_column = tenant_column

        self.sandbox_engine: Engine | None = None
        self.session: Session | None = None
        self._prod_metadata: MetaData | None = None
        self._sandbox_metadata: MetaData | None = None
        self._original_snapshot: dict[str, list[dict[str, Any]]] = {}
        self._clone_stats: dict[str, int] = {}
        self._committed = False

    # ---- Context manager ----

    def __enter__(self) -> ShadowDB:
        self._prod_metadata = reflect_tables(self.prod_engine, self.table_names)

        self.sandbox_engine = create_sandbox_engine()
        self._sandbox_metadata = clone_schema_to_sandbox(
            self._prod_metadata, self.sandbox_engine
        )

        self._clone_stats = clone_rows(
            self.prod_engine,
            self.sandbox_engine,
            self._prod_metadata,
            self._sandbox_metadata,
            self.tenant_column,
            self.tenant_id,
        )

        self._original_snapshot = self._take_snapshot()
        self.session = Session(self.sandbox_engine)
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if self.session:
            self.session.close()
        if self.sandbox_engine:
            self.sandbox_engine.dispose()
        self.sandbox_engine = None
        self.session = None
        return False

    # ---- AI-facing operations ----

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> CursorResult:
        """Execute raw SQL inside the sandbox.

        Convenience wrapper so AI agents don't need to import `text()`.
        """
        self._ensure_open()
        stmt = text(sql)
        if params:
            return self.session.execute(stmt, params)
        return self.session.execute(stmt)

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute a SELECT and return results as a list of dicts."""
        result = self.execute(sql, params)
        return [row._asdict() for row in result.fetchall()]

    # ---- Review & sync ----

    def diff(self) -> ChangeSet:
        """Compute the diff between original cloned data and current sandbox state."""
        self._ensure_open()
        self.session.commit()
        current = self._take_snapshot()
        return compute_diff(
            self._original_snapshot,
            current,
            self._pk_columns(),
        )

    def commit_to_production(self) -> int:
        """Validate and sync all sandbox changes to production atomically.

        Safety gates applied in order:
        1. Row-level diff computation
        2. Pydantic validation on every INSERT/UPDATE row
        3. Tenant ID guard on every row's data
        4. Tenant ID in WHERE clause of every SQL statement
        5. Single atomic transaction (all-or-nothing)

        Returns the number of rows affected.
        Raises SyncError on tenant breach, pydantic.ValidationError on bad data.
        """
        if self._committed:
            raise SyncError("This sandbox has already been committed. Create a new ShadowDB.")

        changeset = self.diff()
        if changeset.is_empty:
            return 0

        affected = apply_changeset(
            self.prod_engine,
            self._prod_metadata,
            changeset,
            self.tenant_column,
            self.tenant_id,
        )
        self._committed = True
        return affected

    # ---- Introspection ----

    @property
    def clone_stats(self) -> dict[str, int]:
        """Number of rows cloned per table at sandbox creation time."""
        return dict(self._clone_stats)

    @property
    def tables(self) -> list[str]:
        """Table names available in this sandbox."""
        return list(self.table_names)

    @property
    def dialect(self) -> str:
        """The production database dialect name (e.g. 'postgresql', 'mysql', 'sqlite')."""
        return self.prod_engine.dialect.name

    # ---- Internals ----

    def _ensure_open(self) -> None:
        if self.session is None or self.sandbox_engine is None:
            raise RuntimeError("ShadowDB is not open. Use it inside a `with` block.")

    def _take_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        snapshot: dict[str, list[dict[str, Any]]] = {}
        with self.sandbox_engine.connect() as conn:
            for table_name, table in self._sandbox_metadata.tables.items():
                rows = conn.execute(select(table)).fetchall()
                snapshot[table_name] = [row._asdict() for row in rows]
        return snapshot

    def _pk_columns(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for table_name, table in self._sandbox_metadata.tables.items():
            result[table_name] = [c.name for c in table.primary_key.columns]
        return result
