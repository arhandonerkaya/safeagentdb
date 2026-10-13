"""
sandbox.py - The main developer-facing API.

Usage:
    with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sandbox:
        sandbox.execute("UPDATE tasks SET status='done' WHERE id=1")
        print(sandbox.diff().display())
        sandbox.commit_to_production()
"""

from __future__ import annotations

import datetime as dt
import decimal
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import CursorResult, ForeignKeyConstraint, MetaData, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from safeagentdb.diff import ChangeSet, compute_diff, normalise_tenant_id, tenant_breach
from safeagentdb.engine import (
    CascadeReference,
    ChangeTracker,
    clone_schema_to_sandbox,
    create_sandbox_engine,
    fetch_rows,
    find_cascade_references,
    install_change_tracking,
    load_rows,
    read_explicit_keys,
    read_key_changes,
    referential_actions,
    reflect_tables,
    schema_signature,
    seed_provisional_keys,
    tracking_problems,
)
from safeagentdb.errors import (
    AgentSQLError,
    AssignedKey,
    ChangesetMismatchError,
    SafeAgentDBError,
    SchemaError,
    SkippedConflict,
    SyncError,
    UsageError,
)
from safeagentdb.sync import OnConflict, _order_diffs, apply_changeset

_VALID_ON_CONFLICT = ("abort", "ignore")


def _bindable_param(value: Any) -> Any:
    """A parameter value SQLite can bind, in the form the sandbox stores it.

    sqlite3 cannot bind a Decimal at all, and binds datetimes in a format that
    differs from the sandbox's ISO 8601 text, so both are turned into text.
    """
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date | dt.time):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


class ShadowDB:
    """Shadow-Sandbox database layer.

    Clones tenant-scoped data into an in-memory SQLite sandbox,
    lets AI operate freely, then syncs approved changes back to
    production in a single atomic transaction.

    PostgreSQL is the first-class production target and is tested in CI.
    SQLite production is supported best-effort, and MySQL is untested; see
    "Supported databases" in the README. The sandbox is always in-memory
    SQLite.

    A ShadowDB that exits without committing can be entered again, and each
    entry takes a fresh clone. One that has committed cannot be entered again,
    and entering one that is already open raises UsageError.

    Args:
        prod_engine: SQLAlchemy Engine connected to the production database.
        tables: List of table names to clone into the sandbox.
        tenant_id: The tenant/user ID to scope all operations to. It is
            converted once, at ``__enter__``, to the Python type of the tenant
            column -- so ``"42"`` from a URL works for an integer column -- and
            refused with UsageError if it does not convert exactly.
        tenant_column: Column name used for tenant isolation (default: "user_id").
        row_key: Optional explicit row-identifying key per table, e.g.
            ``{"events": ["tenant_id", "event_uuid"]}``. Required for tables
            that have no primary key. The columns must exist and must be unique
            across the cloned rows, or SchemaError is raised.
        reference_tables: Tables to clone in full, ignoring the tenant filter.
            Use this for shared lookup tables (statuses, currencies, plans) that
            hold no tenant data, so that foreign keys pointing at them can be
            enforced in the sandbox. Every row of a listed table becomes visible
            to the agent, so only list tables whose contents any tenant may see.
            Reference tables are read-only: changing one raises SyncError.
        on_conflict: What to do when a production row drifted between clone and
            commit. ``"abort"`` (default) raises ConflictError and rolls the
            whole changeset back; ``"ignore"`` skips the drifted row and applies
            the rest.
        require_validators: When True (default), a table with no registered
            SafeModel is an error -- reported as a failed row in ``diff()`` and
            raised as MissingValidatorError at commit time. When False it is a
            warning in both places instead, and the row is written unvalidated.
        allow_cascades: When False (default), the commit refuses with
            CascadeError any write that would make production run a referential
            action (ON DELETE/ON UPDATE CASCADE, SET NULL, SET DEFAULT) on an
            existing row, because that effect is not in the diff that was
            reviewed. When True, such actions may run within the tenant. A row
            in another tenant, or one whose owner cannot be determined, is
            refused either way.

    Raises:
        UsageError: If ``on_conflict`` is not one of the accepted values.
    """

    def __init__(
        self,
        prod_engine: Engine,
        tables: Sequence[str],
        tenant_id: Any,
        tenant_column: str = "user_id",
        *,
        row_key: dict[str, list[str]] | None = None,
        reference_tables: Sequence[str] | None = None,
        on_conflict: OnConflict = "abort",
        require_validators: bool = True,
        allow_cascades: bool = False,
    ) -> None:
        if on_conflict not in _VALID_ON_CONFLICT:
            raise UsageError(
                f"on_conflict must be one of {_VALID_ON_CONFLICT!r}, got {on_conflict!r}."
            )

        self.prod_engine = prod_engine
        self.table_names = list(tables)
        self.tenant_id = tenant_id
        self._requested_tenant_id = tenant_id
        self.tenant_column = tenant_column
        self.row_key = {k: list(v) for k, v in (row_key or {}).items()}
        self.reference_tables = list(reference_tables or ())
        self.on_conflict: OnConflict = on_conflict
        self.require_validators = require_validators
        self.allow_cascades = allow_cascades

        self.sandbox_engine: Engine | None = None
        self.session: Session | None = None
        self._prod_metadata: MetaData | None = None
        self._sandbox_metadata: MetaData | None = None
        self._original_snapshot: dict[str, list[dict[str, Any]]] = {}
        self._clone_stats: dict[str, int] = {}
        self._row_keys: dict[str, list[str]] = {}
        self._unsupported: list[str] = []
        self._cascade_references: list[CascadeReference] = []
        self._converted_columns: dict[str, dict[str, str]] = {}
        self._naive_datetime_columns: dict[str, list[str]] = {}
        self._generated_columns: dict[str, list[str]] = {}
        self._skipped_conflicts: list[SkippedConflict] = []
        self._assigned_keys: list[AssignedKey] = []
        self._provisional_keys: dict[str, list[str]] = {}
        self._provisional_references: dict[str, dict[str, str]] = {}
        self._tracker: ChangeTracker | None = None
        self._schema: tuple[tuple, ...] = ()
        self._committed = False

    # ---- Context manager ----

    def __enter__(self) -> ShadowDB:
        if self.session is not None or self.sandbox_engine is not None:
            raise UsageError(
                "This ShadowDB is already open. Entering it again would discard "
                "the agent's work in the open sandbox; exit it first, or create "
                "a separate ShadowDB."
            )
        if self._committed:
            raise UsageError(
                "This ShadowDB has already committed and cannot be entered again. "
                "Create a new ShadowDB for the next change."
            )

        self.sandbox_engine = create_sandbox_engine()
        try:
            self._open()
        except SafeAgentDBError:
            self._dispose()
            raise
        except SQLAlchemyError as exc:
            self._dispose()
            raise SchemaError(
                f"The sandbox could not be created from production: "
                f"{exc.__class__.__name__}: {exc}. Check that the tables "
                f"{self.table_names + self.reference_tables!r} exist and that "
                f"the production database is reachable."
            ) from exc
        except Exception:
            self._dispose()
            raise

        self.session = Session(self.sandbox_engine)
        return self

    def _open(self) -> None:
        self._prod_metadata = reflect_tables(
            self.prod_engine, list(self.table_names) + self.reference_tables
        )
        self.tenant_id = self._normalised_tenant_id()

        # Read first: which tables end up empty decides which foreign keys
        # the sandbox can honestly enforce.
        fetched = fetch_rows(
            self.prod_engine,
            self._prod_metadata,
            self.tenant_column,
            self.tenant_id,
            self.reference_tables,
        )
        empty_tables = [name for name, rows in fetched.items() if not rows]

        self._sandbox_metadata = clone_schema_to_sandbox(
            self._prod_metadata,
            self.sandbox_engine,
            empty_tables=empty_tables,
            actions=referential_actions(
                self.prod_engine, [t.name for t in self._prod_metadata.tables.values()]
            ),
        )
        self._unsupported = list(
            self._sandbox_metadata.info.get("unsupported_constraints", [])
        )

        # Referential actions reach rows no statement of ours names, so
        # every table in production is inspected, not only the cloned ones.
        self._cascade_references = find_cascade_references(
            self.prod_engine,
            [t.name for t in self._sandbox_metadata.tables.values()],
        )
        self._unsupported.extend(
            reference.describe() for reference in self._cascade_references
        )
        self._generated_columns = dict(
            self._sandbox_metadata.info.get("generated_columns", {})
        )
        self._converted_columns = dict(
            self._sandbox_metadata.info.get("converted_columns", {})
        )
        # SQLite production keeps the offset as text (P2-3), so only a server
        # with a real timestamp-without-time-zone type is checked.
        if self.prod_engine.dialect.name != "sqlite":
            self._naive_datetime_columns = dict(
                self._sandbox_metadata.info.get("naive_datetime_columns", {})
            )

        # Fail fast, before any data is copied, if rows cannot be identified.
        self._row_keys = self._resolve_row_keys()

        self._clone_stats = load_rows(self.sandbox_engine, self._sandbox_metadata, fetched)
        self._provisional_keys = seed_provisional_keys(
            self.sandbox_engine, self._sandbox_metadata
        )
        self._provisional_references = self._find_provisional_references()

        self._original_snapshot = self._take_snapshot()
        self._assert_row_keys_unique(self._original_snapshot)

        # Installed last, so neither loading nor seeding is recorded.
        self._tracker = install_change_tracking(
            self.sandbox_engine,
            self._sandbox_metadata,
            self._row_keys,
            self._provisional_keys,
        )
        self._schema = schema_signature(self.sandbox_engine)

    def _dispose(self) -> None:
        if self.sandbox_engine is not None:
            self.sandbox_engine.dispose()
        self.sandbox_engine = None
        self._tracker = None

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        if self.session:
            self.session.close()
        self._dispose()
        self.session = None
        return False

    # ---- AI-facing operations ----

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> CursorResult:
        """Execute raw SQL inside the sandbox.

        Without ``params`` the string goes to SQLite exactly as written, so a
        literal like ``'{"a":1}'`` or ``'12:30'`` is never read as a bind
        parameter. With ``params`` it is run as a ``text()`` statement with
        ``:name`` placeholders; ``Decimal``, ``datetime``, ``date``, ``time``
        and ``UUID`` values are bound as text.

        Raises:
            AgentSQLError: If the statement fails; the driver's error is the
                ``__cause__``. The message says whether the agent's earlier
                changes survived: usually they do, but ``OR ROLLBACK`` or a
                trigger's ``RAISE(ROLLBACK)`` discards every change since the
                sandbox was opened or last diffed.
            UsageError: Outside the ``with`` block.
        """
        self._ensure_open()
        dbapi = self.session.connection().connection.dbapi_connection
        was_open = dbapi.in_transaction
        try:
            if params:
                bound = {name: _bindable_param(value) for name, value in params.items()}
                return self.session.execute(text(sql), bound)
            return self.session.connection().exec_driver_sql(sql)
        except SQLAlchemyError as exc:
            if was_open and not dbapi.in_transaction:
                outcome = (
                    "SQLite rolled back the sandbox's open transaction with it, so "
                    "every change since the sandbox was opened or last diffed was "
                    "discarded"
                )
            else:
                outcome = "Earlier changes are kept"
            raise AgentSQLError(
                f"The agent's statement failed in the sandbox: "
                f"{getattr(exc, 'orig', None) or exc}. {outcome}."
            ) from exc

    def query(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Execute a statement that returns rows and return them as dicts.

        Raises:
            AgentSQLError: If the statement fails, or returns no rows at all
                (an UPDATE, say -- it has still run; use execute() for those).
        """
        result = self.execute(sql, params)
        if not result.returns_rows:
            raise AgentSQLError(
                "query() expects a statement that returns rows, and this one "
                "returned none. It has been executed; use execute() for "
                "statements that change data."
            )
        try:
            return [row._asdict() for row in result.fetchall()]
        except SQLAlchemyError as exc:
            raise AgentSQLError(f"Reading the agent's query result failed: {exc}") from exc

    # ---- Review & sync ----

    def diff(self) -> ChangeSet:
        """Compute the diff between original cloned data and current sandbox state.

        Raises:
            SchemaError: If the agent changed the sandbox schema (ALTER, DROP,
                CREATE ...), since rows can no longer be compared with the
                clone.
            SyncError: If the agent modified a reference table. Those are shared
                across tenants and are read-only.
            UsageError: Outside the ``with`` block, or after a successful
                commit -- the sandbox no longer differs from production in the
                way the diff would show.
        """
        self._ensure_open()
        if self._committed:
            raise UsageError(
                "This sandbox has already committed its changes. A diff now would "
                "present them for approval again; create a new ShadowDB for the "
                "next change."
            )
        try:
            self.session.commit()
        except SQLAlchemyError as exc:
            raise AgentSQLError(
                f"The sandbox could not settle the agent's open transaction: {exc}"
            ) from exc
        self._assert_schema_unchanged()

        try:
            current = self._take_snapshot()
            key_changes = read_key_changes(self.sandbox_engine, self._tracker)
            explicit = read_explicit_keys(self.sandbox_engine, self._tracker)
        except SQLAlchemyError as exc:
            raise SchemaError(f"The sandbox could not be read: {exc}") from exc
        self._assert_reference_tables_unchanged(current)

        # A row key that no longer identifies a row makes the whole changeset
        # unreadable, so it is reported rather than diffed around.
        blocking = self._row_key_problems(current)
        if blocking:
            return ChangeSet(
                unsupported_constraints=list(self._unsupported),
                blocking_errors=blocking,
            )

        tenant_tables = {
            name: rows
            for name, rows in current.items()
            if not self._is_reference_table(name)
        }
        original = {
            name: rows
            for name, rows in self._original_snapshot.items()
            if not self._is_reference_table(name)
        }
        changeset = compute_diff(
            original,
            tenant_tables,
            self._row_keys,
            unsupported_constraints=self._unsupported,
            require_validators=self.require_validators,
            generated_columns=self._generated_columns,
            provisional_key_columns=self._provisional_keys,
            converted_columns=self._converted_columns,
            explicit_keys=explicit,
            provisional_references=self._provisional_references,
            naive_datetime_columns=self._naive_datetime_columns,
        )
        changeset.blocking_errors.extend(self._key_change_errors(original, current, key_changes))
        changeset.blocking_errors.extend(self._preflight_errors(changeset))
        return changeset

    def commit_to_production(self, changeset: ChangeSet | None = None) -> int:
        """Validate and sync all sandbox changes to production atomically.

        Args:
            changeset: The changeset that was reviewed. When given, the diff is
                recomputed and the commit refuses with ChangesetMismatchError
                if its fingerprint differs, so only what was approved can be
                written. **This is the recommended pattern wherever a human or
                a second agent approves the change** -- without it, anything
                that happened in the sandbox after the review is committed too.
                Omitting it keeps the 0.2.x behaviour of committing whatever
                the sandbox currently holds.

        Safety gates applied in order:
        1. Row-level diff computation, keyed on the primary key or ``row_key``;
           a row key the agent changed is refused
        2. Pydantic validation on every INSERT/UPDATE row
        3. Tenant ID guard on every row's data
        4. Row key + tenant ID in the WHERE clause of every UPDATE/DELETE
        5. Referential actions on rows the diff does not show are refused
           (``allow_cascades``)
        6. Optimistic concurrency check against the clone-time values
        7. Single atomic transaction (all-or-nothing)

        Only the columns the agent actually changed are written.

        Returns the number of rows actually written, summed from each
        statement's rowcount. With ``on_conflict="ignore"`` that can be fewer
        than the changeset contained -- the changeset is applied in part, and
        every skipped row is listed in ``skipped_conflicts`` and warned about.

        Raises:
            CascadeError: If a referential action would act on rows the diff
                does not show, or on another tenant's rows.
            ChangesetMismatchError: If ``changeset`` is not what the sandbox
                now holds.
            ConflictError: If production drifted since the clone and
                ``on_conflict="abort"``.
            GeneratedValueError: If a new row needs a value only production can
                generate, such as a serial or identity primary key.
            IntegrityViolationError: If production rejects a row the sandbox
                accepted, such as a UNIQUE collision with another tenant's row.
            MissingValidatorError: If a table has no SafeModel and
                ``require_validators`` is True.
            RowValidationError: If a row fails its SafeModel.
            SchemaError: If the agent changed the sandbox schema.
            SyncError: On anything ``diff()`` reports as blocking -- a tenant
                breach, a changed row key, rows referencing each other in a
                cycle -- on a second commit, and on any database error.
            UsageError: Outside the ``with`` block, or if ``changeset`` is not
                a ChangeSet.
        """
        if self._committed:
            raise SyncError("This sandbox has already been committed. Create a new ShadowDB.")
        if changeset is not None and not isinstance(changeset, ChangeSet):
            raise UsageError(
                f"changeset must be the ChangeSet returned by diff(), got "
                f"{type(changeset).__name__}."
            )

        reviewed = changeset
        changeset = self.diff()

        if reviewed is not None and reviewed.fingerprint != changeset.fingerprint:
            raise ChangesetMismatchError(
                f"The sandbox changed since this changeset was reviewed, so "
                f"committing it would write something other than what was "
                f"approved. Reviewed {reviewed.summary} with fingerprint "
                f"{reviewed.fingerprint}; the sandbox now holds "
                f"{changeset.summary} with fingerprint {changeset.fingerprint}. "
                f"Review the current diff and pass that instead.",
                reviewed=reviewed.fingerprint,
                current=changeset.fingerprint,
            )

        if changeset.blocking_errors:
            raise SyncError(
                "This changeset cannot be committed:\n  - "
                + "\n  - ".join(changeset.blocking_errors)
            )
        if changeset.is_empty:
            return 0

        self._skipped_conflicts = []
        self._assigned_keys = []
        affected = apply_changeset(
            self.prod_engine,
            self._prod_metadata,
            changeset,
            self.tenant_column,
            self.tenant_id,
            row_keys=self._row_keys,
            on_conflict=self.on_conflict,
            require_validators=self.require_validators,
            cascade_references=self._cascade_references,
            converted_columns=self._converted_columns,
            skipped=self._skipped_conflicts,
            assigned=self._assigned_keys,
            allow_cascades=self.allow_cascades,
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
    def assigned_keys(self) -> list[AssignedKey]:
        """Keys production assigned to rows the sandbox held provisionally.

        A ``serial`` or identity column has no SQLite equivalent, so the sandbox
        fills it with a placeholder and leaves the column out of the INSERT at
        commit time. Each entry pairs the placeholder the diff showed with the
        real key production chose.
        """
        return list(self._assigned_keys)

    @property
    def provisional_key_columns(self) -> dict[str, list[str]]:
        """Key columns the sandbox fills provisionally, per table."""
        return {k: list(v) for k, v in self._provisional_keys.items()}

    @property
    def skipped_conflicts(self) -> list[SkippedConflict]:
        """Rows the last commit left unapplied under ``on_conflict="ignore"``.

        Empty with the default ``on_conflict="abort"``, which never applies a
        changeset in part. Each entry carries the table, the row key, the
        drifted columns and the reason; a ConflictWarning is also emitted per
        skipped row at commit time.
        """
        return list(self._skipped_conflicts)

    @property
    def generated_columns(self) -> dict[str, list[str]]:
        """Columns whose production-side generated default the sandbox lost.

        An INSERT that needs one of these cannot be synced: the sandbox has no
        access to the production sequence or default function.
        """
        return {k: list(v) for k, v in self._generated_columns.items()}

    @property
    def reference_table_names(self) -> list[str]:
        """Tables cloned in full and treated as read-only."""
        return list(self.reference_tables)

    @property
    def cascade_references(self) -> list[CascadeReference]:
        """Foreign keys that make production write rows beyond our statements.

        Each one is also described in ``unsupported_constraints``. The commit
        refuses when the propagated effect would reach another tenant.
        """
        return list(self._cascade_references)

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
            raise UsageError("ShadowDB is not open. Use it inside a `with` block.")

    def _normalised_tenant_id(self) -> Any:
        """The requested tenant id as the tenant column's Python type.

        Done once, here, so every comparison afterwards -- the clone filter, the
        tenant guard, the cascade check -- compares like with like.
        """
        python_types: dict[str, type] = {}
        for table in self._prod_metadata.tables.values():
            if self._is_reference_table(table.name) or self.tenant_column not in table.c:
                continue
            try:
                python_types[table.name] = table.c[self.tenant_column].type.python_type
            except NotImplementedError:
                continue

        distinct = set(python_types.values())
        if not distinct:
            return self._requested_tenant_id
        if len(distinct) > 1:
            described = ", ".join(
                f"{name}: {kind.__name__}" for name, kind in sorted(python_types.items())
            )
            raise SchemaError(
                f"The tenant column '{self.tenant_column}' holds different types in "
                f"different tables ({described}), so one tenant id cannot be "
                f"compared with all of them exactly. Sandbox these tables separately."
            )

        (python_type,) = distinct
        try:
            return normalise_tenant_id(self._requested_tenant_id, python_type)
        except (TypeError, ValueError) as exc:
            raise UsageError(
                f"tenant_id {self._requested_tenant_id!r} cannot be used for "
                f"'{self.tenant_column}', which holds {python_type.__name__} "
                f"values: {exc}. Pass the tenant id as {python_type.__name__}."
            ) from exc

    def _find_provisional_references(self) -> dict[str, dict[str, str]]:
        """{table: {column: parent}} for foreign keys onto a provisional key.

        Read from the production schema, because the sandbox drops a foreign key
        whose parent cloned no rows -- exactly the case of a brand-new parent.
        """
        found: dict[str, dict[str, str]] = {}
        for table in self._prod_metadata.tables.values():
            for constraint in table.constraints:
                if not isinstance(constraint, ForeignKeyConstraint):
                    continue
                if len(constraint.elements) != 1:
                    continue
                (element,) = constraint.elements
                parent, _, column = element.target_fullname.rpartition(".")
                parent = parent.rpartition(".")[2]
                if self._provisional_keys.get(parent) == [column]:
                    found.setdefault(table.name, {})[element.parent.name] = parent
        return found

    def _assert_schema_unchanged(self) -> None:
        """Refuse to diff a sandbox whose schema the agent changed.

        The agent can run any SQL, DDL included. A renamed column or dropped
        table leaves nothing to compare the clone with, and a dropped tracking
        trigger would hide a key change, so either is a clear SchemaError
        rather than a driver error from the snapshot.
        """
        try:
            current = schema_signature(self.sandbox_engine)
            tampered = tracking_problems(self.sandbox_engine, self._tracker)
        except SQLAlchemyError as exc:
            raise SchemaError(f"The sandbox schema could not be read: {exc}") from exc

        if current != self._schema:
            before = {(row[0], row[1]): row[3] for row in self._schema}
            after = {(row[0], row[1]): row[3] for row in current}
            changes = []
            for kind, name in sorted(before.keys() | after.keys()):
                if (kind, name) not in after:
                    changes.append(f"{kind} '{name}' was dropped")
                elif (kind, name) not in before:
                    changes.append(f"{kind} '{name}' was created")
                elif before[(kind, name)] != after[(kind, name)]:
                    changes.append(f"{kind} '{name}' was altered")
            raise SchemaError(
                f"The agent changed the sandbox schema ({'; '.join(changes)}). A "
                f"diff compares rows with the schema that was cloned, so it cannot "
                f"be computed and nothing can be committed. Schema changes belong "
                f"in a migration, not in the agent's session."
            )
        if tampered:
            raise SchemaError(
                f"SafeAgentDB's change tracking in the sandbox was dropped or "
                f"altered ({', '.join(tampered)}), so key changes can no longer be "
                f"detected and nothing can be committed."
            )

    def _key_change_errors(
        self,
        original: dict[str, list[dict[str, Any]]],
        current: dict[str, list[dict[str, Any]]],
        key_changes: dict[str, list[tuple[tuple, tuple | None]]],
    ) -> list[str]:
        """Rows whose row key the agent changed with an UPDATE.

        A diff keys rows on the row key, so a changed key reads as a DELETE of
        the old key and an INSERT of the new one. Production would then run the
        ON DELETE action of every referencing foreign key instead of the ON
        UPDATE action the agent's statement triggers -- deleting children an
        UPDATE would have re-pointed, say. That is not what was reviewed, so it
        is refused. The changes are replayed in order, so a key changed and
        changed back is not reported, a row moved and then deleted is a plain
        DELETE, and a new row reusing a deleted row's key is not that row.
        """
        problems: list[str] = []
        for table_name, moves in sorted(key_changes.items()):
            columns = self._row_keys[table_name]
            originals = {tuple(row[c] for c in columns) for row in original.get(table_name, [])}
            present = {tuple(row[c] for c in columns) for row in current.get(table_name, [])}

            position = {key: key for key in originals}  # original key -> key now
            holder = {key: key for key in originals}  # key now -> original key
            for old, new in moves:
                source = holder.pop(old, None)
                if source is None:
                    continue  # a row the agent inserted itself
                if new is None:
                    del position[source]  # deleted: no longer tracked
                    continue
                position[source] = new
                holder[new] = source

            for source, now in sorted(position.items(), key=repr):
                if now == source or now not in present:
                    continue
                problems.append(
                    f"Row {dict(zip(columns, source, strict=False))!r} in "
                    f"'{table_name}' had its row key changed to "
                    f"{dict(zip(columns, now, strict=False))!r}. SafeAgentDB writes "
                    f"rows by row key, so the change would reach production as a "
                    f"DELETE plus an INSERT, which runs ON DELETE actions instead "
                    f"of ON UPDATE ones and can delete or orphan referencing rows. "
                    f"Key changes must go through the application."
                )
        return problems

    def _take_snapshot(self) -> dict[str, list[dict[str, Any]]]:
        snapshot: dict[str, list[dict[str, Any]]] = {}
        with self.sandbox_engine.connect() as conn:
            for table_name, table in self._sandbox_metadata.tables.items():
                rows = conn.execute(select(table)).fetchall()
                snapshot[table_name] = [row._asdict() for row in rows]
        return snapshot

    def _preflight_errors(self, changeset: ChangeSet) -> list[str]:
        """Everything the commit will refuse that can be known without production.

        The tenant guard used to be commit-only, so diff() showed
        SAFE TO COMMIT for a row that the commit then rejected. These run
        through the same helpers sync uses, so the two cannot drift apart.

        Drift and cross-tenant uniqueness stay commit-only -- they need
        production objects -- which is why the banner says so even when clean.
        """
        problems: list[str] = []
        for diff in changeset.diffs:
            breach = tenant_breach(diff, self.tenant_column, self.tenant_id)
            if breach is not None:
                problems.append(breach)
            if self._is_reference_table(diff.table):
                problems.append(
                    f"Table '{diff.table}' is a reference table and is "
                    f"read-only; it cannot be part of a changeset."
                )

        # Statement order needs only the schema, so a changeset with no order
        # production would accept is reported here, not first at commit.
        try:
            _order_diffs(changeset, self._prod_metadata)
        except SchemaError as exc:
            problems.append(str(exc))
        return problems

    def _row_key_problems(
        self, snapshot: dict[str, list[dict[str, Any]]]
    ) -> list[str]:
        """Row keys that stopped identifying a row since the clone.

        __enter__ checks this against the cloned rows, but the agent can create
        a collision afterwards -- inserting a second row that shares a custom
        row_key, say. Left undetected, the two rows merge into one diff entry
        and the commit overwrites a production row.
        """
        problems: list[str] = []
        for table_name, columns in self._row_keys.items():
            if self._is_reference_table(table_name):
                continue
            seen: set[tuple] = set()
            for row in snapshot.get(table_name, []):
                missing = [c for c in columns if c not in row]
                if missing:
                    problems.append(
                        f"row_key for table '{table_name}' names column(s) "
                        f"{missing!r} that are not present in the rows."
                    )
                    break
                key = tuple(row[c] for c in columns)
                if key in seen:
                    problems.append(
                        f"row_key {columns!r} is not unique in table "
                        f"'{table_name}': the value {key!r} appears more than "
                        f"once in the sandbox, so it cannot identify a row. "
                        f"Nothing can be committed until the duplicate is "
                        f"removed or a wider row_key is used."
                    )
                    break
                seen.add(key)
        return problems

    def _is_reference_table(self, table_name: str) -> bool:
        return table_name in self.reference_tables

    def _assert_reference_tables_unchanged(
        self, current: dict[str, list[dict[str, Any]]]
    ) -> None:
        for table_name in self.reference_tables:
            if table_name not in current:
                continue
            if current[table_name] != self._original_snapshot.get(table_name):
                raise SyncError(
                    f"Table '{table_name}' was listed in reference_tables, which "
                    f"makes it read-only: it is shared across tenants and its rows "
                    f"were cloned in full. The sandbox changed it, so nothing can "
                    f"be committed. Remove it from reference_tables if the agent "
                    f"is meant to write to it."
                )

    def _resolve_row_keys(self) -> dict[str, list[str]]:
        """Decide how each cloned table's rows are identified.

        An explicit ``row_key`` wins; otherwise the primary key is used. A table
        with neither cannot be diffed row by row, so it is rejected outright.
        """
        resolved: dict[str, list[str]] = {}

        for table_name, table in self._sandbox_metadata.tables.items():
            if self._is_reference_table(table_name):
                continue

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
