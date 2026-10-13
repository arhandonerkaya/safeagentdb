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
8. A write that would make production run a referential action (CASCADE,
   SET NULL, SET DEFAULT) on rows the diff does not show is refused, unless
   ``allow_cascades=True``; one that would reach another tenant always is
9. Statements run in the standard unit-of-work order -- INSERTs parents first,
   then UPDATEs, then DELETEs children first, with rows of one table ordered by
   the references between them -- as a deterministic function of the
   changeset, so two concurrent changesets cannot deadlock by touching the
   same rows in opposite orders

Builds every statement, including the cascade check, from SQLAlchemy Core
constructs, so identifiers are quoted by the dialect in use.
"""

from __future__ import annotations

import heapq
import json
import warnings
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import ValidationError
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKeyConstraint,
    LargeBinary,
    MetaData,
    Table,
    Text,
    Time,
    and_,
    delete,
    func,
    insert,
    literal,
    select,
    type_coerce,
    update,
)
from sqlalchemy import column as sql_column
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import table as sql_table
from sqlalchemy.engine import Connection, CursorResult, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from safeagentdb.diff import (
    ChangeSet,
    DiffType,
    RowDiff,
    canonical_value,
    same_tenant,
    tenant_breach,
)
from safeagentdb.engine import (
    DATETIME_CONVERSION,
    CascadeReference,
    SandboxValueError,
    decode_sandbox_value,
)
from safeagentdb.errors import (
    AssignedKey,
    CascadeError,
    ConflictError,
    ConflictWarning,
    DuplicateRowKeyError,
    GeneratedValueError,
    IntegrityViolationError,
    RowValidationError,
    SafeAgentDBError,
    SchemaError,
    SkippedConflict,
    SyncError,
)
from safeagentdb.models import format_validation_error, validate_row

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
    allow_cascades: bool = False,
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
            checked for referencing rows before it runs.
        converted_columns: {table: {column: conversion}} for columns the sandbox
            had to store in a different representation. Their values are turned
            back before being written.
        skipped: A list to append one SkippedConflict to per row skipped under
            ``on_conflict="ignore"``. Without it a partial apply leaves no
            record of what was left out.
        assigned: A list to append one AssignedKey to per row whose provisional
            key production replaced with a real one.
        allow_cascades: When False (default), a write that would make
            production run a referential action on any existing row raises
            CascadeError, because that effect is not in the diff. When True, it
            is allowed within the tenant; another tenant's row, or a row whose
            owner cannot be determined, is refused either way.

    Returns the number of rows actually written, summed from each statement's
    rowcount. Rows production changed through an allowed referential action
    are not counted.

    Raises:
        ConflictError: If a row drifted or vanished and ``on_conflict="abort"``.
        CascadeError: If a referential action would act on rows the diff does
            not show (see ``allow_cascades``).
        GeneratedValueError: If a row needs a value only production can generate.
        IntegrityViolationError: If production rejects a row the sandbox accepted,
            such as a UNIQUE collision with another tenant's row.
        RowValidationError: If a row fails its SafeModel.
        SchemaError: If rows reference each other in a cycle.
        SyncError: On tenant breach, a missing row key, an unknown table, a
            value production's type cannot take, or any database error.
        MissingValidatorError: If a table has no registered SafeModel.
    """
    if changeset.is_empty:
        return 0

    try:
        return _apply(
            engine,
            metadata,
            changeset,
            tenant_column,
            tenant_id,
            row_keys=row_keys,
            on_conflict=on_conflict,
            require_validators=require_validators,
            cascade_references=cascade_references,
            converted_columns=converted_columns or {},
            skipped=skipped,
            assigned=assigned,
            allow_cascades=allow_cascades,
        )
    except SafeAgentDBError:
        raise
    except SQLAlchemyError as exc:
        # Starting the transaction, the existing-key read before an INSERT, the
        # cascade check and the conflict diagnosis all talk to production. A
        # failure in any of them is the same outage as a failed write.
        raise SyncError(
            f"The database raised an error while the changeset was being "
            f"applied: {_driver_message(exc)}. The whole changeset was rolled "
            f"back; nothing was written."
        ) from exc


def _apply(
    engine: Engine,
    metadata: MetaData,
    changeset: ChangeSet,
    tenant_column: str,
    tenant_id: Any,
    *,
    row_keys: dict[str, list[str]] | None,
    on_conflict: OnConflict,
    require_validators: bool,
    cascade_references: list[CascadeReference] | None,
    converted_columns: dict[str, dict[str, str]],
    skipped: list[SkippedConflict] | None,
    assigned: list[AssignedKey] | None,
    allow_cascades: bool,
) -> int:
    affected = 0
    dialect = engine.dialect.name
    ordered = _order_diffs(changeset, metadata)
    # {child table: action} for referential actions allow_cascades let run, so
    # a later statement that then matches nothing is not blamed on another
    # writer.
    cascaded: dict[str, str] = {}

    with engine.begin() as conn:
        for diff in ordered:
            table = metadata.tables.get(diff.table)
            if table is None:
                raise SyncError(f"Table '{diff.table}' not found in production metadata.")
            converted = converted_columns.get(diff.table, {})

            # ---- Gate 1: Tenant guard on row data ----
            # Same helper ShadowDB.diff() calls, so a breach is visible in the
            # dashboard rather than only here.
            breach = tenant_breach(diff, tenant_column, tenant_id)
            if breach is not None:
                raise SyncError(breach)

            if diff.diff_type in (DiffType.INSERT, DiffType.UPDATE):
                undecodable = diff.undecodable_columns()
                if undecodable:
                    raise SyncError(diff.undecodable_message(undecodable))

                # ---- Gate 2: Pydantic validation ----
                # The SafeModel describes production, so a column the sandbox
                # had to store differently is converted back first. Same helper
                # RowDiff.validate() uses, so the two cannot disagree.
                _validate(diff, require_validators)

            # ---- Gate 2b: refuse values only production can generate ----
            blocked = diff.blocked_generated_columns()
            if blocked:
                raise GeneratedValueError(
                    diff.generated_value_message(blocked),
                    table=diff.table,
                    columns=blocked,
                )

            regenerated = diff.regenerated_key_columns()
            if regenerated:
                raise GeneratedValueError(
                    diff.regenerated_key_message(regenerated),
                    table=diff.table,
                    columns=regenerated,
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
                _refuse_existing_key(
                    conn, table, diff, tenant_column, tenant_id, row_keys, converted, dialect
                )

                # A key the sandbox only held provisionally is left out, so the
                # production sequence assigns the real one.
                pending = diff.pending_key_columns()
                values = _production_values(
                    table,
                    {c: v for c, v in (diff.new or {}).items() if c not in pending},
                    converted,
                    dialect,
                )
                result = _execute(conn, insert(table).values(values), diff, diff.pk)
                if pending:
                    _record_assignment(result, table, diff, pending, assigned)
                affected += max(result.rowcount, 0)
                continue

            # ---- Gate 4: never touch rows without identifying them ----
            key = _require_row_key(diff, table, tenant_column, tenant_id, row_keys)

            # ---- Gate 4b: no writes beyond what the diff shows ----
            _refuse_cascade(
                conn,
                diff,
                key,
                tenant_column,
                tenant_id,
                cascade_references,
                allow_cascades,
                cascaded,
            )

            # ---- Gate 5: compare and swap ----
            # The clone-time values go into the WHERE clause, so the check and
            # the write are one statement. Nothing can slip in between them.
            guarded = _guard_columns(diff, table, key, converted, dialect)

            stmt = update(table) if diff.diff_type == DiffType.UPDATE else delete(table)
            for key_col, key_val in key.items():
                stmt = stmt.where(
                    _predicate(table.c[key_col], key_val, converted.get(key_col), dialect)
                )
            stmt = stmt.where(table.c[tenant_column] == tenant_id)
            for col in guarded:
                stmt = stmt.where(
                    _predicate(table.c[col], (diff.old or {})[col], converted.get(col), dialect)
                )

            if diff.diff_type == DiffType.UPDATE:
                # Only the columns the agent actually touched, so a column it
                # never looked at is never overwritten.
                changed = {
                    col: diff.new[col] for col in diff.changed_columns() if col in table.c
                }
                if not changed:
                    continue
                stmt = stmt.values(_production_values(table, changed, converted, dialect))

            result = _execute(conn, stmt, diff, key)

            if result.rowcount == 0:
                # The guard did not match. Re-read -- no lock needed, the write
                # already did not happen -- only to say why.
                conflict = _diagnose_conflict(
                    conn,
                    table,
                    diff,
                    key,
                    tenant_column,
                    tenant_id,
                    guarded,
                    converted,
                    dialect,
                    cascaded,
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


def _validate(diff: RowDiff, require_validators: bool) -> None:
    try:
        validate_row(
            diff.table,
            diff.logical_row(diff.new),
            require_validator=require_validators,
        )
    except ValidationError as exc:
        raise RowValidationError(
            f"Row {diff.pk!r} in '{diff.table}' failed validation: "
            f"{format_validation_error(exc)}. The whole changeset was rolled back.",
            table=diff.table,
            row_key=diff.pk,
            errors=exc.errors(),
        ) from exc


# ---- Values and predicates ----


def _sqlite_timestamp_text(value: Any) -> str:
    """A datetime in the text form SQLAlchemy's SQLite DATETIME writes, plus offset.

    SQLAlchemy itself would drop the offset; keeping its format otherwise means
    a naive value lands exactly as it always did.
    """
    text = (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d} "
        f"{value.hour:02d}:{value.minute:02d}:{value.second:02d}."
        f"{value.microsecond:06d}"
    )
    offset = value.utcoffset()
    if offset is None:
        return text
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{text}{sign}{total // 3600:02d}:{total % 3600 // 60:02d}"


def _production_value(value: Any, conversion: str | None, dialect: str) -> Any:
    """A sandbox value as production's column should receive it."""
    if conversion is None or value is None:
        return value
    decoded = decode_sandbox_value(value, conversion)
    if conversion == DATETIME_CONVERSION and dialect == "sqlite":
        # Bound as text so the UTC offset survives SQLite's DATETIME type.
        return literal(_sqlite_timestamp_text(decoded), type_=Text())
    return decoded


def _production_values(
    table: Table, values: dict[str, Any], converted: dict[str, str], dialect: str
) -> dict[str, Any]:
    return {
        col: _production_value(value, converted.get(col), dialect)
        for col, value in values.items()
        if col in table.c
    }


def _predicate(column: Column, value: Any, conversion: str | None, dialect: str):
    """Equality with a clone-time value, as production stores it.

    A SQLite production datetime was read as its stored text, so it is matched
    as that text exactly; anything else is matched as its production value.
    """
    if value is None:
        return column.is_(None)
    if conversion == DATETIME_CONVERSION and dialect == "sqlite":
        return type_coerce(column, Text()) == value
    if conversion is not None:
        try:
            value = decode_sandbox_value(value, conversion)
        except SandboxValueError:
            pass
    return _matches(column, value)


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


# ---- Referential actions ----


def _refuse_cascade(
    conn: Connection,
    diff: RowDiff,
    key: dict[str, Any],
    tenant_column: str,
    tenant_id: Any,
    references: list[CascadeReference] | None,
    allow_cascades: bool,
    cascaded: dict[str, str],
) -> None:
    """Refuse a write whose referential action reaches rows the diff does not show.

    SafeAgentDB scopes the statements it issues. A propagating foreign key makes
    the database act on further rows itself, and neither the tenant predicate
    nor the diff covers those. So before deleting a referenced row, or changing
    a referenced column, the referencing rows are counted inside the same
    transaction. Rows the changeset already deleted or re-pointed are gone by
    then -- they ran earlier -- so a cascade the diff shows in full passes.
    """
    if not references:
        return

    relevant = [r for r in references if r.parent_table == diff.table]
    if not relevant:
        return

    old = diff.logical_row(diff.old)

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

        found = _referencing_rows(conn, reference, values, tenant_column, tenant_id)

        if found.unattributed:
            # An owner we cannot determine is not an owner we may assume. An
            # audit log keyed only on task_id is the common shape here.
            raise CascadeError(
                f"Refusing to {diff.diff_type.value} row {key!r} in "
                f"'{diff.table}': '{reference.child_table}' references it "
                f"{action}, and {found.unattributed} row(s) it holds for this key "
                f"cannot be attributed to any tenant (the table has no "
                f"'{tenant_column}' column, or it is NULL). SafeAgentDB will not "
                f"treat an unknown owner as this tenant, so the whole changeset "
                f"was rolled back. If '{reference.child_table}' genuinely belongs "
                f"to whichever row it references, the change has to be made "
                f"through the application.",
                table=diff.table,
                referencing_table=reference.child_table,
                unattributed_rows=found.unattributed,
                action=action,
            )

        if found.tenants:
            tenants = sorted(str(t) for t in found.tenants)
            raise CascadeError(
                f"Refusing to {diff.diff_type.value} row {key!r} in "
                f"'{diff.table}': '{reference.child_table}' references it "
                f"{action}, so production would also act on rows there "
                f"belonging to {len(found.tenants)} other tenant(s) "
                f"({tenant_column} in {tenants}). That is outside the tenant "
                f"scope SafeAgentDB can enforce, so the whole changeset was "
                f"rolled back.",
                table=diff.table,
                referencing_table=reference.child_table,
                tenants=sorted(found.tenants, key=str),
                action=action,
            )

        if found.own:
            if not allow_cascades:
                raise CascadeError(
                    f"Refusing to {diff.diff_type.value} row {key!r} in "
                    f"'{diff.table}': '{reference.child_table}' references it "
                    f"{action}, so production would also act on {found.own} "
                    f"row(s) there that this changeset does not show. A commit "
                    f"writes what was reviewed and nothing else, so the whole "
                    f"changeset was rolled back. Change those rows in the sandbox "
                    f"first (clone '{reference.child_table}' to see them), or "
                    f"pass allow_cascades=True to let production run referential "
                    f"actions within this tenant.",
                    table=diff.table,
                    referencing_table=reference.child_table,
                    action=action,
                )
            cascaded[reference.child_table] = (
                f"{action} from the {diff.diff_type.value} of {key!r} in '{diff.table}'"
            )


@dataclass(frozen=True)
class _ReferencingRows:
    """What a propagating foreign key would reach, beyond our own statement."""

    own: int
    tenants: set[Any]
    unattributed: int


def _referencing_rows_statement(
    reference: CascadeReference, values: list[Any], tenant_column: str | None
):
    """The count of rows referencing ``values``, per tenant when there is one.

    Built from Core constructs rather than f-string SQL, so identifiers are
    quoted by the dialect -- double quotes broke on MySQL without ANSI_QUOTES.
    The child table may be absent from our metadata, so a lightweight table
    clause names only the columns needed.
    """
    names = list(reference.child_columns)
    if tenant_column and tenant_column not in names:
        names.append(tenant_column)
    child = sql_table(reference.child_table, *[sql_column(name) for name in names])
    condition = and_(
        *[
            child.c[col] == value
            for col, value in zip(reference.child_columns, values, strict=False)
        ]
    )
    if tenant_column is None:
        return select(func.count()).select_from(child).where(condition)
    tenant = child.c[tenant_column]
    return select(tenant, func.count()).where(condition).group_by(tenant)


def _referencing_rows(
    conn: Connection,
    reference: CascadeReference,
    values: list[Any],
    tenant_column: str,
    tenant_id: Any,
) -> _ReferencingRows:
    """Rows referencing the given key, split by whose they are.

    ``own`` counts the acting tenant's rows, ``tenants`` holds the other
    tenants that own any, and ``unattributed`` counts rows whose owner cannot
    be established -- the referencing table has no tenant column (an audit log
    keyed only on task_id, say), or the value is NULL.
    """
    columns = {col["name"] for col in sa_inspect(conn).get_columns(reference.child_table)}

    if tenant_column not in columns:
        stmt = _referencing_rows_statement(reference, values, None)
        count = conn.execute(stmt).scalar_one()
        return _ReferencingRows(own=0, tenants=set(), unattributed=int(count or 0))

    own = 0
    unattributed = 0
    tenants: set[Any] = set()
    stmt = _referencing_rows_statement(reference, values, tenant_column)
    for value, count in conn.execute(stmt):
        if value is None:
            unattributed += int(count)
        elif same_tenant(value, tenant_id):
            own += int(count)
        else:
            tenants.add(value)
    return _ReferencingRows(own=own, tenants=tenants, unattributed=unattributed)


def _refuse_existing_key(
    conn: Connection,
    table: Table,
    diff: RowDiff,
    tenant_column: str,
    tenant_id: Any,
    row_keys: dict[str, list[str]] | None,
    converted: dict[str, str] | None = None,
    dialect: str = "",
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
        stmt = stmt.where(_predicate(table.c[col], value, (converted or {}).get(col), dialect))
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


# ---- Statement order ----


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
    graph: dict[str, set[str]] = {
        table.name: set() for table in metadata.tables.values()
    }
    for child, _cols, parent, _parent_cols in _foreign_keys(metadata):
        if parent != child:
            graph[child].add(parent)
    return graph


def _foreign_keys(
    metadata: MetaData,
) -> list[tuple[str, tuple[str, ...], str, tuple[str, ...]]]:
    """(child, child columns, parent, parent columns) for every foreign key
    whose parent is also in this metadata."""
    known = {table.name for table in metadata.tables.values()}
    found = []
    for table in metadata.tables.values():
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint) or not constraint.elements:
                continue
            targets = [e.target_fullname.rsplit(".", 1) for e in constraint.elements]
            parent = targets[0][0].rsplit(".", 1)[-1]
            if parent not in known:
                continue
            found.append(
                (
                    table.name,
                    tuple(e.parent.name for e in constraint.elements),
                    parent,
                    tuple(t[1] for t in targets),
                )
            )
    return found


# The standard unit-of-work order. INSERTs come first, parents before
# children, so every new row's parent exists; UPDATEs next, so a child is
# re-pointed before its old parent goes; DELETEs last, children before parents.
_PHASES = (DiffType.INSERT, DiffType.UPDATE, DiffType.DELETE)


def _order_diffs(changeset: ChangeSet, metadata: MetaData) -> list[RowDiff]:
    """The changeset in an order production will accept.

    Within the INSERT and DELETE phases rows are ordered by the references
    between them, across tables and inside one table alike, so a manager is
    inserted before a report keyed below it and deleted after it. Ties fall to
    the topological table order and then the row key, so the result is a total
    deterministic function of the changeset: two concurrent commits take shared
    rows in the same order and cannot deadlock against each other. Rows that
    reference each other in a cycle have no such order and are refused.
    """
    try:
        order = table_write_order(_dependencies(metadata))
    except SchemaError:
        # A cycle between tables need not be a cycle between these rows; the
        # row-level ordering below decides, and refuses a real one.
        order = sorted(table.name for table in metadata.tables.values())
    rank = {name: index for index, name in enumerate(order)}
    depth = len(rank)
    fks = _foreign_keys(metadata)

    def tie_break(diff: RowDiff) -> tuple:
        position = rank.get(diff.table, depth)
        if diff.diff_type == DiffType.DELETE:
            position = depth - position
        return (
            position,
            diff.table,
            tuple(sorted((k, repr(v)) for k, v in (diff.pk or {}).items())),
        )

    ordered: list[RowDiff] = []
    for phase in _PHASES:
        group = sorted(
            (d for d in changeset.diffs if d.diff_type == phase), key=tie_break
        )
        if phase == DiffType.UPDATE:
            ordered.extend(group)
        else:
            ordered.extend(_order_rows(group, fks, tie_break, phase))
    return ordered


def _reference_key(values: list[Any]) -> str:
    return json.dumps(canonical_value(list(values)), sort_keys=True)


def _order_rows(
    group: list[RowDiff],
    fks: list[tuple[str, tuple[str, ...], str, tuple[str, ...]]],
    tie_break: Any,
    phase: DiffType,
) -> list[RowDiff]:
    rows = [(d.new if phase == DiffType.INSERT else d.old) or {} for d in group]

    index: dict[tuple[str, tuple[str, ...]], dict[str, list[int]]] = {}
    targets = {(parent, parent_cols) for _c, _cc, parent, parent_cols in fks}
    for position, diff in enumerate(group):
        for parent, parent_cols in targets:
            if diff.table != parent:
                continue
            values = [rows[position].get(c) for c in parent_cols]
            if any(v is None for v in values):
                continue
            index.setdefault((parent, parent_cols), {}).setdefault(
                _reference_key(values), []
            ).append(position)

    # before[i]: rows that must be written before row i.
    before: list[set[int]] = [set() for _ in group]
    for position, diff in enumerate(group):
        for child, child_cols, parent, parent_cols in fks:
            if diff.table != child:
                continue
            values = [rows[position].get(c) for c in child_cols]
            if any(v is None for v in values):
                continue
            for other in index.get((parent, parent_cols), {}).get(_reference_key(values), []):
                if other == position:
                    continue  # a row referencing itself is one statement
                if phase == DiffType.INSERT:
                    before[position].add(other)
                else:
                    before[other].add(position)

    waiting = [set(deps) for deps in before]
    dependents: list[set[int]] = [set() for _ in group]
    for position, deps in enumerate(before):
        for dep in deps:
            dependents[dep].add(position)

    heap = [(tie_break(group[i]), i) for i, deps in enumerate(waiting) if not deps]
    heapq.heapify(heap)
    result: list[RowDiff] = []
    done: set[int] = set()
    while heap:
        _, position = heapq.heappop(heap)
        done.add(position)
        result.append(group[position])
        for dependent in dependents[position]:
            waiting[dependent].discard(position)
            if not waiting[dependent] and dependent not in done:
                heapq.heappush(heap, (tie_break(group[dependent]), dependent))

    if len(result) != len(group):
        stuck = [group[i] for i in range(len(group)) if i not in done]
        described = ", ".join(f"'{d.table}' {d.pk!r}" for d in stuck)
        raise SchemaError(
            f"These rows reference each other in a cycle: {described}. There is "
            f"no order in which production would accept the {phase.value}s one "
            f"statement at a time, so SafeAgentDB will not guess one. Commit "
            f"them in separate changesets, or break the cycle with a NULL first."
        )
    return result


# ---- Compare and swap ----


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

# SQLite stores these as text or numbers in whatever format the writer chose
# ('2024-01-01 10:00:00', '2024-01-01T10:00:00', 'true', 1) and compares that
# storage literally. A clone-time value bound back through the column type is
# written in SQLAlchemy's format, so an equality on it can fail with nothing
# changed -- a false ConflictError that blames a writer who does not exist.
# PostgreSQL and MySQL compare typed values and keep the guard.
_SQLITE_TEXT_COMPARED_TYPES = (Date, DateTime, Time, Boolean)


def _is_guardable(column: Column) -> bool:
    if isinstance(column.type, _UNGUARDABLE_TYPES):
        return False
    return type(column.type).__name__.upper() not in _UNGUARDABLE_TYPE_NAMES


def _guard_columns(
    diff: RowDiff,
    table: Table,
    key: dict[str, Any],
    converted: dict[str, str] | None = None,
    dialect: str | None = None,
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

    conversions = converted or {}
    guarded = []
    for col in candidates:
        if col not in old or col in key or col not in table.c:
            continue
        conversion = conversions.get(col)
        # A JSON or text-mapped value is compared by meaning, not storage. A
        # datetime is compared as a datetime, except on SQLite (see above).
        if conversion is not None and conversion != DATETIME_CONVERSION:
            continue
        if dialect == "sqlite" and (
            conversion == DATETIME_CONVERSION
            or isinstance(table.c[col].type, _SQLITE_TEXT_COMPARED_TYPES)
        ):
            continue
        if not _is_guardable(table.c[col]):
            continue
        guarded.append(col)
    return guarded


def _execute(
    conn: Connection,
    stmt: Any,
    diff: RowDiff,
    row_key: dict[str, Any],
) -> CursorResult:
    """Run one statement, turning database errors into SafeAgentDBError.

    Without this, an IntegrityError from production escapes the documented
    exception hierarchy, so the `except SafeAgentDBError` the README tells
    people to write would miss it. A bind-time StatementError, raised before
    the statement reaches the database, is wrapped too.
    """
    try:
        return conn.execute(stmt)
    except IntegrityError as exc:
        # Say what production rejected, and the possible reasons, without
        # claiming one: from here there is no way to tell which it was.
        raise IntegrityViolationError(
            f"Production rejected the {diff.diff_type.value} on '{diff.table}' "
            f"for row {row_key!r}: {_driver_message(exc)}. The whole changeset "
            f"was rolled back. The sandbox accepted this row, so production "
            f"enforces something the tenant-scoped clone could not check -- "
            f"typically a row in another tenant or in a table that was not "
            f"cloned -- or the statement order SafeAgentDB uses (INSERTs, then "
            f"UPDATEs, then DELETEs) does not suit this changeset, as when a "
            f"unique value moves from a deleted row to a new one.",
            table=diff.table,
            row_key=row_key,
        ) from exc
    except SQLAlchemyError as exc:
        raise SyncError(
            f"The database rejected the {diff.diff_type.value} on '{diff.table}' "
            f"for row {row_key!r}: {_driver_message(exc)}. The whole changeset "
            f"was rolled back."
        ) from exc


def _driver_message(exc: SQLAlchemyError) -> str:
    orig = getattr(exc, "orig", None)
    return str(orig) if orig is not None else str(exc)


def _diagnose_conflict(
    conn: Connection,
    table: Table,
    diff: RowDiff,
    key: dict[str, Any],
    tenant_column: str,
    tenant_id: Any,
    guarded: list[str],
    converted: dict[str, str] | None = None,
    dialect: str = "",
    cascaded: dict[str, str] | None = None,
) -> ConflictError:
    """Explain why a guarded statement matched nothing.

    Only ever called after the write has already failed to match, so this read
    needs no lock: it cannot change the outcome, only describe it. It never
    names a concurrent writer it has no evidence for.
    """
    conversions = converted or {}
    stmt = select(table)
    for key_col, key_val in key.items():
        stmt = stmt.where(_predicate(table.c[key_col], key_val, conversions.get(key_col), dialect))
    stmt = stmt.where(table.c[tenant_column] == tenant_id)

    rows = conn.execute(stmt.limit(2)).fetchall()

    earlier = (cascaded or {}).get(diff.table)
    if earlier:
        return ConflictError(
            f"Row {key!r} in '{diff.table}' no longer matched its clone-time "
            f"values, so the {diff.diff_type.value} matched nothing. An earlier "
            f"statement in this changeset ran {earlier}, which production "
            f"applies to '{diff.table}' by itself and may have changed or "
            f"removed this row (allowed by allow_cascades=True). The whole "
            f"changeset was not applied as reviewed.",
            table=diff.table,
            row_key=key,
        )

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
    original = diff.logical_row(diff.old)
    drifted = sorted(
        col
        for col in guarded
        if col in current and current[col] != original.get(col)
    )

    if not drifted:
        # Re-reading shows the clone-time values, so there is no evidence of
        # another writer. Either the row changed and changed back between the
        # write and this read, or the database compared a stored value in a
        # format other than the one bound for it.
        return ConflictError(
            f"The {diff.diff_type.value} of row {key!r} in '{diff.table}' matched "
            f"nothing, yet re-reading the row shows its clone-time values. "
            f"SafeAgentDB cannot name a column that changed, so it does not "
            f"assume another writer: the stored value may be in a format the "
            f"database compares differently from the one bound for it, or the "
            f"row changed and changed back. Nothing was written.",
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
