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
    ForeignKeyConstraint,
    LargeBinary,
    MetaData,
    Table,
    delete,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.engine import Connection, CursorResult, Engine
from sqlalchemy.exc import DBAPIError, IntegrityError

from safeagentdb.diff import ChangeSet, DiffType, RowDiff, tenant_breach
from safeagentdb.engine import CascadeReference, convert_row_for_production
from safeagentdb.errors import (
    AssignedKey,
    CascadeError,
    ConflictError,
    ConflictWarning,
    DuplicateRowKeyError,
    GeneratedValueError,
    IntegrityViolationError,
    SchemaError,
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
    cascade_references: list[CascadeReference] | None = None,
    converted_columns: dict[str, dict[str, str]] | None = None,
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
        cascade_references: Foreign keys that propagate into the written tables.
            Each DELETE, and each UPDATE touching a referenced column, is
            checked for referencing rows in other tenants before it runs.
        converted_columns: {table: {column: conversion}} for columns the sandbox
            had to store in a different representation. Their values are turned
            back before being written.
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
        for diff in _order_diffs(changeset, metadata):
            table = metadata.tables.get(diff.table)
            if table is None:
                raise SyncError(f"Table '{diff.table}' not found in production metadata.")

            # ---- Gate 1: Tenant guard on row data ----
            # Same helper ShadowDB.diff() calls, so a breach is visible in the
            # dashboard rather than only here.
            breach = tenant_breach(diff, tenant_column, tenant_id)
            if breach is not None:
                raise SyncError(breach)

            if diff.diff_type in (DiffType.INSERT, DiffType.UPDATE):
                row_data = diff.new

                # ---- Gate 2: Pydantic validation ----
                # The SafeModel describes production, so a column the sandbox
                # had to store differently is converted back first. Same helper
                # RowDiff.validate() uses, so the two cannot disagree.
                validate_row(
                    diff.table,
                    diff.logical_row(row_data),
                    require_validator=require_validators,
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
                # An INSERT must not quietly become an overwrite, so a key
                # production already holds is refused here rather than later.
                _refuse_existing_key(conn, table, diff, tenant_column, tenant_id, row_keys)

                # A key the sandbox only held provisionally is left out, so the
                # production sequence assigns the real one.
                pending = diff.pending_key_columns()
                values = {
                    col: value
                    for col, value in (diff.new or {}).items()
                    if col not in pending
                }
                values = convert_row_for_production(
                    values, (converted_columns or {}).get(diff.table, {})
                )
                result = _execute(conn, insert(table).values(values), diff, diff.pk)
                if pending:
                    _record_assignment(result, table, diff, pending, assigned)
                affected += max(result.rowcount, 0)
                continue

            # ---- Gate 4: never touch rows without identifying them ----
            key = _require_row_key(diff, table, tenant_column, tenant_id, row_keys)

            # ---- Gate 4b: no indirect writes outside the tenant ----
            _refuse_cross_tenant_cascade(
                conn, diff, key, tenant_column, tenant_id, cascade_references
            )

            # ---- Gate 5: compare and swap ----
            # The clone-time values go into the WHERE clause, so the check and
            # the write are one statement. Nothing can slip in between them.
            guarded = _guard_columns(
                diff, table, key, (converted_columns or {}).get(diff.table, {})
            )

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
                values = convert_row_for_production(
                    values, (converted_columns or {}).get(diff.table, {})
                )
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


def _refuse_cross_tenant_cascade(
    conn: Connection,
    diff: RowDiff,
    key: dict[str, Any],
    tenant_column: str,
    tenant_id: Any,
    references: list[CascadeReference] | None,
) -> None:
    """Refuse a write whose referential action would reach another tenant.

    SafeAgentDB scopes the statements it issues. A propagating foreign key makes
    the database act on further rows itself, and the tenant predicate in our
    WHERE clause has no bearing on those. So before deleting a referenced row,
    or changing a referenced column, the referencing rows are counted per tenant
    inside the same transaction.
    """
    if not references:
        return

    relevant = [r for r in references if r.parent_table == diff.table]
    if not relevant:
        return

    old = diff.old or {}

    for reference in relevant:
        if diff.diff_type == DiffType.DELETE:
            if not reference.on_delete:
                continue
            action = f"ON DELETE {reference.on_delete}"
        else:
            # An UPDATE only propagates when it changes a referenced column.
            if not reference.on_update:
                continue
            touched = set(diff.changed_columns()) & set(reference.parent_columns)
            if not touched:
                continue
            action = f"ON UPDATE {reference.on_update}"

        values = [old.get(col) for col in reference.parent_columns]
        if any(value is None for value in values):
            continue

        affected = _referencing_tenants(
            conn, reference, values, tenant_column, tenant_id
        )
        if affected is None:
            continue
        if affected:
            tenants = sorted(str(t) for t in affected)
            raise CascadeError(
                f"Refusing to {diff.diff_type.value} row {key!r} in "
                f"'{diff.table}': '{reference.child_table}' references it "
                f"{action}, so production would also act on "
                f"{len(affected)} other tenant(s) worth of rows there "
                f"({tenant_column} in {tenants}). That is outside the tenant "
                f"scope SafeAgentDB can enforce, so the whole changeset was "
                f"rolled back.",
                table=diff.table,
                referencing_table=reference.child_table,
                tenants=sorted(affected, key=str),
                action=action,
            )


def _referencing_tenants(
    conn: Connection,
    reference: CascadeReference,
    values: list[Any],
    tenant_column: str,
    tenant_id: Any,
) -> set[Any] | None:
    """Tenants other than ours holding rows that reference the given key.

    Returns an empty set when every referencing row is ours, and None when the
    question cannot be asked -- a child table with no tenant column, where a
    non-empty result is reported as the sentinel below instead.
    """
    child = reference.child_table
    predicates = " AND ".join(
        f'"{column}" = :v{index}'
        for index, column in enumerate(reference.child_columns)
    )
    params = {f"v{index}": value for index, value in enumerate(values)}

    # The child table may not be in our metadata at all, so its columns are
    # read from the database rather than from a Table object.
    columns = set(
        conn.exec_driver_sql(f'SELECT * FROM "{child}" WHERE 1=0').keys()
    )

    if tenant_column not in columns:
        # No tenant column: any referencing row is outside the sandbox's scope.
        count = conn.execute(
            text(f'SELECT COUNT(*) FROM "{child}" WHERE {predicates}'), params
        ).scalar_one()
        return {f"<no {tenant_column} column>"} if count else set()

    rows = conn.execute(
        text(
            f'SELECT DISTINCT "{tenant_column}" FROM "{child}" WHERE {predicates}'
        ),
        params,
    ).fetchall()
    return {row[0] for row in rows if row[0] != tenant_id}


def _refuse_existing_key(
    conn: Connection,
    table: Table,
    diff: RowDiff,
    tenant_column: str,
    tenant_id: Any,
    row_keys: dict[str, list[str]] | None,
) -> None:
    """Refuse an INSERT whose row key production already holds.

    Without this the row key is never consulted on an INSERT, so a changeset
    carrying a duplicate key reached production and the row that landed
    depended on which statement ran last.
    """
    expected = (row_keys or {}).get(diff.table)
    key = {
        col: value
        for col, value in (diff.pk or {}).items()
        if col in table.c
    }
    if not key or not expected:
        return
    if set(key) != set(expected):
        return
    # A key production assigns is not known yet, so there is nothing to check.
    if diff.pending_key_columns():
        return

    stmt = select(table)
    for col, value in key.items():
        stmt = stmt.where(_matches(table.c[col], value))
    if tenant_column in table.c:
        stmt = stmt.where(table.c[tenant_column] == tenant_id)

    if conn.execute(stmt.limit(1)).fetchone() is not None:
        raise DuplicateRowKeyError(
            f"Refusing to INSERT into '{diff.table}': a row with key {key!r} "
            f"already exists in production. Applying it would overwrite that "
            f"row rather than add one.",
            table=diff.table,
            row_key=key,
        )


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


def table_write_order(dependencies: dict[str, set[str]]) -> list[str]:
    """Tables ordered parents first, from {table: tables it references}.

    Kahn's algorithm, with ties broken by name so the result is a deterministic
    function of the schema. A cycle has no safe order, so it is refused rather
    than attempted -- guessing would mean emitting a statement production is
    certain to reject.
    """
    remaining = {table: set(deps) & set(dependencies) for table, deps in dependencies.items()}
    ordered: list[str] = []

    while remaining:
        ready = sorted(t for t, deps in remaining.items() if not deps)
        if not ready:
            cycle = sorted(remaining)
            raise SchemaError(
                f"These tables form a foreign-key dependency cycle: {cycle}. "
                f"There is no order in which they can be written that production "
                f"would accept, so SafeAgentDB will not guess one. Break the "
                f"cycle, or commit the tables in separate changesets."
            )
        for table in ready:
            ordered.append(table)
            del remaining[table]
        for deps in remaining.values():
            deps.difference_update(ready)

    return ordered


def _dependencies(metadata: MetaData) -> dict[str, set[str]]:
    """{table: the tables its foreign keys point at}, within this metadata."""
    known = {table.name for table in metadata.tables.values()}
    graph: dict[str, set[str]] = {name: set() for name in known}

    for table in metadata.tables.values():
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            for element in constraint.elements:
                parent = element.target_fullname.rsplit(".", 1)[0]
                if parent in known and parent != table.name:
                    graph[table.name].add(parent)

    return graph


# DELETEs run first and children before parents; then INSERTs and UPDATEs,
# parents before children. A row deleted and re-inserted under the same key
# therefore still goes in the right order.
_PHASE = {DiffType.DELETE: 0, DiffType.INSERT: 1, DiffType.UPDATE: 2}


def _order_diffs(changeset: ChangeSet, metadata: MetaData) -> list[RowDiff]:
    """The changeset in an order production will accept.

    Alphabetical table order put a child before its parent, so a perfectly
    valid changeset was rejected by the database. Ordering is now topological
    on the foreign-key graph, and still a total deterministic function of the
    changeset, so two concurrent commits take shared rows in the same order and
    cannot deadlock against each other.
    """
    order = table_write_order(_dependencies(metadata))
    rank = {name: index for index, name in enumerate(order)}
    depth = len(rank)

    def key(diff: RowDiff) -> tuple:
        phase = _PHASE[diff.diff_type]
        position = rank.get(diff.table, depth)
        # Children before parents when removing, parents first otherwise.
        if diff.diff_type == DiffType.DELETE:
            position = depth - position
        return (
            phase,
            position,
            diff.table,
            tuple(sorted((k, repr(v)) for k, v in (diff.pk or {}).items())),
        )

    return sorted(changeset.diffs, key=key)


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


def _guard_columns(
    diff: RowDiff,
    table: Table,
    key: dict[str, Any],
    converted: dict[str, str] | None = None,
) -> list[str]:
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

    remapped = set(converted or ())
    return [
        col
        for col in candidates
        if col in old
        and col not in key
        and col not in remapped
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
        # Say what production rejected, not why. The reason could be a
        # constraint whose other side is outside this tenant, or a column the
        # sandbox had to store differently -- and from here there is no way to
        # tell which, so claiming one would be a guess the caller then trusts.
        raise IntegrityViolationError(
            f"Production rejected the {diff.diff_type.value} on '{diff.table}' "
            f"for row {row_key!r}: {_driver_message(exc)}. The whole changeset "
            f"was rolled back. The sandbox accepted this row, so the rule that "
            f"rejected it is one the tenant-scoped clone could not evaluate.",
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
