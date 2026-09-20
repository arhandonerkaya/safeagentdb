"""
sync.py - Atomic sync engine that applies approved changesets to production.

Safety guarantees:
1. Every row is re-validated through Pydantic before write
2. Entire changeset is applied in a single transaction (atomic)
3. Tenant ID is re-checked on every row to prevent scope escape
4. Tenant ID is enforced in the WHERE clause of every UPDATE/DELETE
5. No UPDATE or DELETE is ever executed without a row-identifying predicate

Uses only SQLAlchemy Core constructs -- no raw SQL, no dialect-specific
hacks. Works identically on PostgreSQL, MySQL, and SQLite.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import MetaData, Table, delete, insert, update
from sqlalchemy.engine import Engine

from safeagentdb.diff import ChangeSet, DiffType, RowDiff
from safeagentdb.errors import SyncError
from safeagentdb.models import validate_row


def apply_changeset(
    engine: Engine,
    metadata: MetaData,
    changeset: ChangeSet,
    tenant_column: str,
    tenant_id: Any,
    *,
    row_keys: dict[str, list[str]] | None = None,
) -> int:
    """Apply an approved changeset to the production database atomically.

    The entire changeset runs inside engine.begin() -- a single
    transaction. If ANY row fails validation or tenant checks, the
    entire transaction rolls back and nothing is written.

    Args:
        row_keys: Expected row-identifying columns per table. When given, each
            UPDATE/DELETE diff must carry exactly those columns in its key.

    Returns the number of rows affected.
    Raises SyncError if any tenant check fails, or if an UPDATE/DELETE would
    run without a row-identifying predicate.
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
                affected += 1
                continue

            # ---- Gate 4: never touch rows without identifying them ----
            key = _require_row_key(diff, table, tenant_column, tenant_id, row_keys)

            stmt = update(table) if diff.diff_type == DiffType.UPDATE else delete(table)
            for key_col, key_val in key.items():
                stmt = stmt.where(table.c[key_col] == key_val)
            stmt = stmt.where(table.c[tenant_column] == tenant_id)

            if diff.diff_type == DiffType.UPDATE:
                stmt = stmt.values(diff.new)
            conn.execute(stmt)

            affected += 1

    return affected


def _require_row_key(
    diff: RowDiff,
    table: Table,
    tenant_column: str,
    tenant_id: Any,
    row_keys: dict[str, list[str]] | None,
) -> dict[str, Any]:
    """Return the row-identifying key for an UPDATE/DELETE, or refuse to run it.

    Without this guard an empty key collapses the WHERE clause down to the
    tenant predicate alone, and the statement rewrites or deletes every row the
    tenant owns. Nothing below this point may execute without a row key.
    """
    key = dict(diff.pk or {})

    if not key:
        raise SyncError(
            f"Refusing to {diff.diff_type.value} rows in '{diff.table}' without a "
            f"row-identifying key: the statement would match every row with "
            f"{tenant_column}={tenant_id!r}. This usually means the table has no "
            f"primary key -- pass row_key={{'{diff.table}': [...]}} to ShadowDB."
        )

    missing = [col for col in key if col not in table.c]
    if missing:
        raise SyncError(
            f"Row key for '{diff.table}' names column(s) {missing!r} that do not "
            f"exist in the production table."
        )

    expected = row_keys.get(diff.table) if row_keys else None
    if expected is not None and set(key) != set(expected):
        raise SyncError(
            f"Row key mismatch on '{diff.table}': expected columns "
            f"{sorted(expected)!r}, changeset carried {sorted(key)!r}."
        )

    return key
