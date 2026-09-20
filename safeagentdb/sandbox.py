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
from safeagentdb.errors import SchemaError, SyncError
from safeagentdb.sync import OnConflict, apply_changeset

_VALID_ON_CONFLICT = ("abort", "ignore")


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
        row_key: Optional explicit row-identifying key per table, e.g.
            ``{"events": ["tenant_id", "event_uuid"]}``. Required for tables
            that have no primary key. The columns must exist and must be unique
            across the cloned rows, or SchemaError is raised.
        on_conflict: What to do when a production row drifted between clone and
            commit. ``"abort"`` (default) raises ConflictError and rolls the
            whole changeset back; ``"ignore"`` skips the drifted row and applies
            the rest.
        require_validators: When True (default), a table with no registered
            SafeModel is an error -- reported as a failed row in ``diff()`` and
            raised as MissingValidatorError at commit time. When False it is a
            warning in both places instead, and the row is written unvalidated.

    Raises:
        SchemaError: If a cloned table has no primary key and no usable
            ``row_key``, or if the production schema cannot be reproduced.
    """

    def __init__(
        self,
        prod_engine: Engine,
        tables: Sequence[str],
        tenant_id: Any,
        tenant_column: str = "user_id",
        *,
        row_key: dict[str, list[str]] | None = None,
        on_conflict: OnConflict = "abort",
        require_validators: bool = True,
    ) -> None:
        if on_conflict not in _VALID_ON_CONFLICT:
            raise ValueError(
                f"on_conflict must be one of {_VALID_ON_CONFLICT!r}, got {on_conflict!r}."
            )

        self.prod_engine = prod_engine
        self.table_names = list(tables)
        self.tenant_id = tenant_id
        self.tenant_column = tenant_column
        self.row_key = {k: list(v) for k, v in (row_key or {}).items()}
        self.on_conflict: OnConflict = on_conflict
        self.require_validators = require_validators

        self.sandbox_engine: Engine | None = None
        self.session: Session | None = None
        self._prod_metadata: MetaData | None = None
        self._sandbox_metadata: MetaData | None = None
        self._original_snapshot: dict[str, list[dict[str, Any]]] = {}
        self._clone_stats: dict[str, int] = {}
        self._row_keys: dict[str, list[str]] = {}
        self._unsupported: list[str] = []
        self._committed = False

    # ---- Context manager ----

    def __enter__(self) -> ShadowDB:
        self._prod_metadata = reflect_tables(self.prod_engine, self.table_names)

        self.sandbox_engine = create_sandbox_engine()
        try:
            self._sandbox_metadata = clone_schema_to_sandbox(
                self._prod_metadata, self.sandbox_engine
            )
            self._unsupported = list(
                self._sandbox_metadata.info.get("unsupported_constraints", [])
            )

            # Fail fast, before any data is copied, if rows cannot be identified.
            self._row_keys = self._resolve_row_keys()

            self._clone_stats = clone_rows(
                self.prod_engine,
                self.sandbox_engine,
                self._prod_metadata,
                self._sandbox_metadata,
                self.tenant_column,
                self.tenant_id,
            )

            self._original_snapshot = self._take_snapshot()
            self._assert_row_keys_unique(self._original_snapshot)
        except Exception:
            self.sandbox_engine.dispose()
            self.sandbox_engine = None
            raise

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
            self._row_keys,
            unsupported_constraints=self._unsupported,
            require_validators=self.require_validators,
        )

    def commit_to_production(self) -> int:
        """Validate and sync all sandbox changes to production atomically.

        Safety gates applied in order:
        1. Row-level diff computation, keyed on the primary key or ``row_key``
        2. Pydantic validation on every INSERT/UPDATE row
        3. Tenant ID guard on every row's data
        4. Row key + tenant ID in the WHERE clause of every UPDATE/DELETE
        5. Optimistic concurrency check against the clone-time values
        6. Single atomic transaction (all-or-nothing)

        Only the columns the agent actually changed are written.

        Returns the number of rows actually written, summed from each
        statement's rowcount.

        Raises:
            ConflictError: If production drifted since the clone and
                ``on_conflict="abort"``.
            SyncError: On tenant breach or a missing row key.
            MissingValidatorError: If a table has no SafeModel and
                ``require_validators`` is True.
            pydantic.ValidationError: If a row fails schema validation.
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
            row_keys=self._row_keys,
            on_conflict=self.on_conflict,
            require_validators=self.require_validators,
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

    @property
    def unsupported_constraints(self) -> list[str]:
        """Schema elements that could not be reproduced in the SQLite sandbox.

        Each entry is a human-readable description. Anything listed here is NOT
        enforced inside the sandbox, so a violation of it will only be caught by
        production at commit time.
        """
        return list(self._unsupported)

    @property
    def row_keys(self) -> dict[str, list[str]]:
        """The row-identifying columns in use for each cloned table."""
        return {k: list(v) for k, v in self._row_keys.items()}

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

    def _resolve_row_keys(self) -> dict[str, list[str]]:
        """Decide how each cloned table's rows are identified.

        An explicit ``row_key`` wins; otherwise the primary key is used. A table
        with neither cannot be diffed row by row, so it is rejected outright.
        """
        resolved: dict[str, list[str]] = {}

        for table_name, table in self._sandbox_metadata.tables.items():
            supplied = self.row_key.get(table_name)
            if supplied:
                missing = [c for c in supplied if c not in table.c]
                if missing:
                    raise SchemaError(
                        f"row_key for table '{table_name}' names column(s) "
                        f"{missing!r} that do not exist. Available columns: "
                        f"{sorted(c.name for c in table.c)}."
                    )
                resolved[table_name] = list(supplied)
                continue

            pk_columns = [c.name for c in table.primary_key.columns]
            if not pk_columns:
                raise SchemaError(
                    f"Table '{table_name}' has no primary key, so SafeAgentDB cannot "
                    f"tell its rows apart and cannot diff them safely. Pass an "
                    f"explicit unique key, e.g. "
                    f"ShadowDB(..., row_key={{'{table_name}': ['col_a', 'col_b']}}), "
                    f"or exclude the table from `tables`."
                )
            resolved[table_name] = pk_columns

        return resolved

    def _assert_row_keys_unique(
        self, snapshot: dict[str, list[dict[str, Any]]]
    ) -> None:
        """A row key that repeats cannot identify a row, so reject it."""
        for table_name, columns in self._row_keys.items():
            seen: set[tuple] = set()
            for row in snapshot.get(table_name, []):
                missing = [c for c in columns if c not in row]
                if missing:
                    raise SchemaError(
                        f"row_key for table '{table_name}' names column(s) "
                        f"{missing!r} that are not present in the cloned rows."
                    )
                key = tuple(row[c] for c in columns)
                if key in seen:
                    raise SchemaError(
                        f"row_key {columns!r} is not unique in table '{table_name}': "
                        f"the value {key!r} appears more than once among the rows "
                        f"cloned for {self.tenant_column}={self.tenant_id!r}. "
                        f"Choose columns that uniquely identify a row."
                    )
                seen.add(key)
