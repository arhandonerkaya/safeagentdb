"""
sync.py - Atomic sync engine that applies approved changesets to production.

Safety guarantees:
1. Every row is re-validated through Pydantic before write
2. Entire changeset is applied in a single transaction (atomic)
3. Tenant ID is re-checked on every row to prevent scope escape
4. Tenant ID is enforced in the WHERE clause of every UPDATE/DELETE
5. No UPDATE or DELETE is ever executed without a row-identifying predicate
6. Every UPDATE/DELETE carries its clone-time values in the WHERE clause, so
   the check and the write are one atomic statement with no window between
   them, whatever the isolation level
7. Only the columns the agent actually changed are written
8. Statements run in a deterministic order, so two concurrent changesets
   cannot deadlock by touching the same rows in opposite orders

Uses only SQLAlchemy Core constructs -- no raw SQL, no dialect-specific
hacks. Works identically on PostgreSQL, MySQL, and SQLite.
"""

from __future__ import annotations

import warnings
from typing import Any, Literal

from sqlalchemy import (
    JSON,
    Column,
    Float,
    LargeBinary,
    MetaData,
    Table,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, CursorResult, Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from safeagentdb.diff import ChangeSet, DiffType, RowDiff
from safeagentdb.errors import (
    AssignedKey,
    ConflictError,
    ConflictWarning,
    GeneratedValueError,
    IntegrityViolationError,
    SkippedConflict,
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
    skipped: list[SkippedConflict] | None = None,
    assigned: list[AssignedKey] | None = None,
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
        skipped: A list to append one SkippedConflict to per row skipped under
            ``on_conflict="ignore"``. Without it a partial apply leaves no
            record of what was left out.
        assigned: A list to append one AssignedKey to per row whose provisional
            key production replaced with a real one.

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
        for diff in sorted(changeset.diffs, key=_statement_order):
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

            dangling = diff.provisional_reference_columns()
            if dangling:
                raise GeneratedValueError(
                    diff.provisional_reference_message(dangling),
                    table=diff.table,
                    columns=dangling,
                )

            # ---- Gate 3: Execute with tenant-scoped WHERE ----
            if diff.diff_type == DiffType.INSERT:
                # A key the sandbox only held provisionally is left out, so the
                # production sequence assigns the real one.
                pending = diff.pending_key_columns()
                values = {
                    col: value
                    for col, value in (diff.new or {}).items()
                    if col not in pending
                }
                result = _execute(conn, insert(table).values(values), diff, diff.pk)
                if pending:
                    _record_assignment(result, table, diff, pending, assigned)
                affected += max(result.rowcount, 0)
                continue

            # ---- Gate 4: never touch rows without identifying them ----
            key = _require_row_key(diff, table, tenant_column, tenant_id, row_keys)

            # ---- Gate 5: compare and swap ----
            # The clone-time values go into the WHERE clause, so the check and
            # the write are one statement. Nothing can slip in between them.
            guarded = _guard_columns(diff, table, key)

            stmt = update(table) if diff.diff_type == DiffType.UPDATE else delete(table)
            for key_col, key_val in key.items():
                stmt = stmt.where(_matches(table.c[key_col], key_val))
            stmt = stmt.where(table.c[tenant_column] == tenant_id)
            for col in guarded:
                stmt = stmt.where(_matches(table.c[col], (diff.old or {})[col]))

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
                # The guard did not match. Re-read -- no lock needed, the write
                # already did not happen -- only to say why.
                conflict = _diagnose_conflict(
                    conn, table, diff, key, tenant_column, tenant_id, guarded
                )
                if on_conflict == "ignore":
                    _record_skip(conflict, skipped)
                    continue
                raise conflict

            if result.rowcount > 1:
                raise ConflictError(
                    f"{diff.diff_type.value} on '{diff.table}' matched "
                    f"{result.rowcount} rows for key {key!r}, so that key does not "
                    f"identify a row in production. The changeset was rolled back.",
                    table=diff.table,
                    row_key=key,
                )

            affected += result.rowcount

    return affected


def _record_assignment(
    result: CursorResult,
    table: Table,
    diff: RowDiff,
    pending: list[str],
    assigned: list[AssignedKey] | None,
) -> None:
    """Read back the key production chose, so the caller learns the real id.

    SQLAlchemy sources this from RETURNING where the dialect supports it and
    from the driver's lastrowid otherwise, so it works on PostgreSQL, MySQL and
    SQLite alike.
    """
    if assigned is None:
        return

    try:
        returned = result.inserted_primary_key
    except Exception:  # pragma: no cover -- driver without key retrieval
        returned = None

    real: dict[str, Any] = {}
    if returned is not None:
        pk_names = [col.name for col in table.primary_key.columns]
        real = {
            name: value
            for name, value in zip(pk_names, returned, strict=False)
            if name in pending
        }

    assigned.append(
        AssignedKey(
            table=diff.table,
            provisional={col: (diff.new or {}).get(col) for col in pending},
            assigned=real,
        )
    )


def _record_skip(
    conflict: ConflictError, skipped: list[SkippedConflict] | None
) -> None:
    """Never drop a skipped row silently: record it and say so."""
    entry = SkippedConflict(
        table=conflict.table or "",
        row_key=dict(conflict.row_key),
        columns=tuple(conflict.columns),
        reason=str(conflict),
    )
    if skipped is not None:
        skipped.append(entry)

    warnings.warn(
        f"Skipped a drifted row under on_conflict='ignore': {conflict}",
        ConflictWarning,
        stacklevel=2,
    )


def _statement_order(diff: RowDiff) -> tuple:
    """Total order over a changeset: table, then row key, then operation.

    Two concurrent changesets touching the same rows therefore take them in the
    same order and cannot deadlock against each other.
    """
    return (
        diff.table,
        tuple(sorted((k, repr(v)) for k, v in (diff.pk or {}).items())),
        diff.diff_type.value,
    )


# Types whose equality does not survive a driver round-trip reliably enough to
# gate a write on. Guarding a float on exact equality would reject correct
# changesets; guarding a JSON blob would compare formatting, not meaning.
_UNGUARDABLE_TYPES = (Float, LargeBinary, JSON)
_UNGUARDABLE_TYPE_NAMES = frozenset(
    {
        "ARRAY",
        "BLOB",
        "BYTEA",
        "HSTORE",
        "JSON",
        "JSONB",
        "MONEY",
        "TSVECTOR",
    }
)


def _is_guardable(column: Column) -> bool:
    if isinstance(column.type, _UNGUARDABLE_TYPES):
        return False
    return type(column.type).__name__.upper() not in _UNGUARDABLE_TYPE_NAMES


def _guard_columns(diff: RowDiff, table: Table, key: dict[str, Any]) -> list[str]:
    """Columns whose clone-time value is asserted in the WHERE clause.

    For an UPDATE only the columns the agent changed are guarded, so an
    unrelated concurrent edit to a different column of the same row is allowed
    to coexist. For a DELETE there are no changed columns, so the whole row is
    guarded: removing a row somebody else just edited is itself a lost update.
    """
    old = diff.old or {}
    if diff.diff_type == DiffType.UPDATE:
        candidates = diff.changed_columns()
    else:
        candidates = list(old)

    return [
        col
        for col in candidates
        if col in old
        and col not in key
        and col in table.c
        and _is_guardable(table.c[col])
    ]


def _matches(column: Column, value: Any):
    """An equality predicate that is correct for NULL.

    ``column = NULL`` is never true, so a NULL clone-time value has to become
    ``column IS NULL``. Because the clone-time value is a Python value we hold,
    not an unknown bind parameter, this plain branch is exact everywhere and
    needs no IS NOT DISTINCT FROM / <=> dialect handling.
    """
    if value is None:
        return column.is_(None)
    return column == value


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


def _diagnose_conflict(
    conn: Connection,
    table: Table,
    diff: RowDiff,
    key: dict[str, Any],
    tenant_column: str,
    tenant_id: Any,
    guarded: list[str],
) -> ConflictError:
    """Explain why a guarded statement matched nothing.

    Only ever called after the write has already failed to match, so this read
    needs no lock: it cannot change the outcome, only describe it.
    """
    stmt = select(table)
    for key_col, key_val in key.items():
        stmt = stmt.where(_matches(table.c[key_col], key_val))
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
        for col in guarded
        if col in current and current[col] != original.get(col)
    )

    if not drifted:
        # The row is back to its cloned values, or changed again between the
        # write and this read. Either way the write did not happen.
        return ConflictError(
            f"Row {key!r} in '{diff.table}' changed in production after the "
            f"sandbox was created, so the {diff.diff_type.value} matched nothing.",
            table=diff.table,
            row_key=key,
        )

    return ConflictError(
        f"Row {key!r} in '{diff.table}' changed in production after the "
        f"sandbox was created; column(s) {drifted!r} no longer hold the "
        f"cloned values. Re-clone and re-apply, or pass "
        f'on_conflict="ignore" to skip drifted rows.',
        table=diff.table,
        row_key=key,
        columns=drifted,
    )


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
