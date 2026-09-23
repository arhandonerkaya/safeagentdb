"""
sync.py - Atomic sync engine that applies approved changesets to production.

Safety guarantees:
1. Every row is re-validated through Pydantic before write
2. Entire changeset is applied in a single transaction (atomic)
3. Tenant ID is re-checked on every row to prevent scope escape
4. Tenant ID is enforced in the WHERE clause of every UPDATE/DELETE
5. No UPDATE or DELETE is ever executed without a row-identifying predicate
6. Every UPDATE/DELETE target is re-read and compared against the values
   captured at clone time, so a concurrent write is never silently lost
7. Only the columns the agent actually changed are written

Uses only SQLAlchemy Core constructs -- no raw SQL, no dialect-specific
hacks. Works identically on PostgreSQL, MySQL, and SQLite.
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import MetaData, Table, delete, insert, select, update
from sqlalchemy.engine import Connection, CursorResult, Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from safeagentdb.diff import ChangeSet, DiffType, RowDiff
from safeagentdb.errors import (
    ConflictError,
    GeneratedValueError,
    IntegrityViolationError,
    SyncError,
)
from safeagentdb.models import validate_row

OnConflict = Literal["abort", "ignore"]


def apply_changeset(
    engine: Engine,
    metadata: MetaData,
    changeset: ChangeSet,
    tenant_column: str,
    tenant_id: Any,
    *,
    row_keys: dict[str, list[str]] | None = None,
    on_conflict: OnConflict = "abort",
    require_validators: bool = True,
) -> int:
    """Apply an approved changeset to the production database atomically.

    The entire changeset runs inside engine.begin() -- a single
    transaction. If ANY row fails validation, tenant checks or the
    concurrency check, the entire transaction rolls back and nothing is
    written.

    Args:
        row_keys: Expected row-identifying columns per table. When given, each
            UPDATE/DELETE diff must carry exactly those columns in its key.
        on_conflict: ``"abort"`` raises ConflictError when a production row
            drifted since the clone; ``"ignore"`` skips that row and applies
            the rest.
        require_validators: When True (default), a table with no registered
            SafeModel raises MissingValidatorError. When False it warns and the
            row is written unvalidated -- matching RowDiff.validate() exactly.

    Returns the number of rows actually written, summed from each statement's
    rowcount.

    Raises:
        ConflictError: If a row drifted or vanished and ``on_conflict="abort"``.
        GeneratedValueError: If a row needs a value only production can generate.
        IntegrityViolationError: If production rejects a row the sandbox accepted,
            such as a UNIQUE collision with another tenant's row.
        SyncError: On tenant breach, a missing row key, or an unknown table.
        MissingValidatorError: If a table has no registered SafeModel.
        pydantic.ValidationError: If any row fails schema validation.
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
                validate_row(
                    diff.table, row_data, require_validator=require_validators
                )

            elif diff.diff_type == DiffType.DELETE:
                if diff.old and diff.old.get(tenant_column) != tenant_id:
                    raise SyncError(
                        f"Tenant breach blocked on DELETE: row in '{diff.table}' "
                        f"belongs to {tenant_column}={diff.old.get(tenant_column)!r}."
                    )

            # ---- Gate 2b: refuse values only production can generate ----
            blocked = diff.blocked_generated_columns()
            if blocked:
                raise GeneratedValueError(
                    diff.generated_value_message(blocked),
                    table=diff.table,
                    columns=blocked,
                )

            # ---- Gate 3: Execute with tenant-scoped WHERE ----
            if diff.diff_type == DiffType.INSERT:
                result = _execute(conn, insert(table).values(diff.new), diff, diff.pk)
                affected += max(result.rowcount, 0)
                continue

            # ---- Gate 4: never touch rows without identifying them ----
            key = _require_row_key(diff, table, tenant_column, tenant_id, row_keys)

            # ---- Gate 5: optimistic concurrency check ----
            conflict = _detect_conflict(
                conn, table, diff, key, tenant_column, tenant_id
            )
            if conflict is not None:
                if on_conflict == "ignore":
                    continue
                raise conflict

            stmt = update(table) if diff.diff_type == DiffType.UPDATE else delete(table)
            for key_col, key_val in key.items():
                stmt = stmt.where(table.c[key_col] == key_val)
            stmt = stmt.where(table.c[tenant_column] == tenant_id)

            if diff.diff_type == DiffType.UPDATE:
                # Only the columns the agent actually touched, so a column it
                # never looked at is never overwritten.
                values = {
                    col: diff.new[col]
                    for col in diff.changed_columns()
                    if col in table.c
                }
                if not values:
                    continue
                stmt = stmt.values(values)

            result = _execute(conn, stmt, diff, key)

            if result.rowcount == 0:
                if on_conflict == "ignore":
                    continue
                raise ConflictError(
                    f"{diff.diff_type.value} on '{diff.table}' matched no rows: "
                    f"the row {key!r} for {tenant_column}={tenant_id!r} no longer "
                    f"exists in production.",
                    table=diff.table,
                    row_key=key,
                )

            affected += result.rowcount

    return affected


def _execute(
    conn: Connection,
    stmt: Any,
    diff: RowDiff,
    row_key: dict[str, Any],
) -> CursorResult:
    """Run one statement, turning driver errors into SafeAgentDBError.

    Without this, an IntegrityError from production escapes the documented
    exception hierarchy, so the `except SafeAgentDBError` the README tells
    people to write would miss it.
    """
    try:
        return conn.execute(stmt)
    except IntegrityError as exc:
        raise IntegrityViolationError(
            f"Production rejected the {diff.diff_type.value} on '{diff.table}' "
            f"for row {row_key!r}: {_driver_message(exc)}. The sandbox could not "
            f"catch this because it holds only this tenant's rows. The whole "
            f"changeset was rolled back.",
            table=diff.table,
            row_key=row_key,
        ) from exc
    except DBAPIError as exc:
        raise SyncError(
            f"The database rejected the {diff.diff_type.value} on '{diff.table}' "
            f"for row {row_key!r}: {_driver_message(exc)}. The whole changeset "
            f"was rolled back."
        ) from exc


def _driver_message(exc: DBAPIError) -> str:
    return str(exc.orig) if exc.orig is not None else str(exc)


def _detect_conflict(
    conn: Connection,
    table: Table,
    diff: RowDiff,
    key: dict[str, Any],
    tenant_column: str,
    tenant_id: Any,
) -> ConflictError | None:
    """Compare the live production row against the values captured at clone time.

    Returns a ConflictError describing the drift, or None when the row still
    looks exactly as it did when the sandbox was created.
    """
    stmt = select(table)
    for key_col, key_val in key.items():
        stmt = stmt.where(table.c[key_col] == key_val)
    stmt = stmt.where(table.c[tenant_column] == tenant_id)

    rows = conn.execute(stmt.limit(2)).fetchall()

    if not rows:
        return ConflictError(
            f"Row {key!r} in '{diff.table}' was deleted in production after the "
            f"sandbox was created, so the {diff.diff_type.value} cannot be applied.",
            table=diff.table,
            row_key=key,
        )

    if len(rows) > 1:
        return ConflictError(
            f"Row key {sorted(key)!r} matches more than one row in '{diff.table}' "
            f"for {tenant_column}={tenant_id!r}, so it cannot identify a row.",
            table=diff.table,
            row_key=key,
        )

    current = rows[0]._asdict()
    original = diff.old or {}
    drifted = sorted(
        col
        for col, cloned_value in original.items()
        if col in current and current[col] != cloned_value
    )

    if drifted:
        return ConflictError(
            f"Row {key!r} in '{diff.table}' changed in production after the "
            f"sandbox was created; column(s) {drifted!r} no longer hold the "
            f"cloned values. Re-clone and re-apply, or pass "
            f'on_conflict="ignore" to skip drifted rows.',
            table=diff.table,
            row_key=key,
            columns=drifted,
        )

    return None


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
