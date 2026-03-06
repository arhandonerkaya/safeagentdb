"""
sync.py - Atomic sync engine that applies approved changesets to production.

Safety guarantees:
1. Every row is re-validated through Pydantic before write
2. Entire changeset is applied in a single transaction (atomic)
3. Tenant ID is re-checked on every row to prevent scope escape
4. Tenant ID is enforced in the WHERE clause of every UPDATE/DELETE

Uses only SQLAlchemy Core constructs -- no raw SQL, no dialect-specific
hacks. Works identically on PostgreSQL, MySQL, and SQLite.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import MetaData, delete, insert, update
from sqlalchemy.engine import Engine

from safeagentdb.diff import ChangeSet, DiffType
from safeagentdb.models import validate_row


class SyncError(Exception):
    """Raised when sync validation or execution fails."""


def apply_changeset(
    engine: Engine,
    metadata: MetaData,
    changeset: ChangeSet,
    tenant_column: str,
    tenant_id: Any,
) -> int:
    """Apply an approved changeset to the production database atomically.

    The entire changeset runs inside engine.begin() -- a single
    transaction. If ANY row fails validation or tenant checks, the
    entire transaction rolls back and nothing is written.

    Returns the number of rows affected.
    Raises SyncError if any tenant check fails.
    Raises pydantic.ValidationError if any row fails schema validation.
    """
    if changeset.is_empty:
        return 0

    affected = 0

    with engine.begin() as conn:
        for diff in changeset.diffs:
            table = metadata.tables.get(diff.table)
            if table is None:
                raise SyncError(f"Table '{diff.table}' not found in production metadata.")

            # ---- Gate 1: Tenant guard on row data ----
            if diff.diff_type in (DiffType.INSERT, DiffType.UPDATE):
                row_data = diff.new
                if row_data is None:
                    raise SyncError(
                        f"Missing row data for {diff.diff_type.value} on '{diff.table}'."
                    )

                if row_data.get(tenant_column) != tenant_id:
                    raise SyncError(
                        f"Tenant breach blocked on {diff.diff_type.value}: "
                        f"row has {tenant_column}={row_data.get(tenant_column)!r}, "
                        f"expected {tenant_id!r}."
                    )

                # ---- Gate 2: Pydantic validation ----
                validate_row(diff.table, row_data)

            elif diff.diff_type == DiffType.DELETE:
                if diff.old and diff.old.get(tenant_column) != tenant_id:
                    raise SyncError(
                        f"Tenant breach blocked on DELETE: row in '{diff.table}' "
                        f"belongs to {tenant_column}={diff.old.get(tenant_column)!r}."
                    )

            # ---- Gate 3: Execute with tenant-scoped WHERE ----
            if diff.diff_type == DiffType.INSERT:
                conn.execute(insert(table).values(diff.new))

            elif diff.diff_type == DiffType.UPDATE:
                stmt = update(table)
                for pk_col, pk_val in diff.pk.items():
                    stmt = stmt.where(table.c[pk_col] == pk_val)
                stmt = stmt.where(table.c[tenant_column] == tenant_id)
                conn.execute(stmt.values(diff.new))

            elif diff.diff_type == DiffType.DELETE:
                stmt = delete(table)
                for pk_col, pk_val in diff.pk.items():
                    stmt = stmt.where(table.c[pk_col] == pk_val)
                stmt = stmt.where(table.c[tenant_column] == tenant_id)
                conn.execute(stmt)

            affected += 1

    return affected
