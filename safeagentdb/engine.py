"""
engine.py - Manages production DB connections and in-memory sandbox creation.

Handles dialect-agnostic schema reflection and type mapping so that tables
from PostgreSQL, MySQL, or any SQLAlchemy-supported DB can be cloned into
an in-memory SQLite sandbox without errors.

The clone starts from ``Table.to_metadata()``, which carries constraints,
indexes and defaults across intact, and then rewrites only the parts SQLite
cannot express: dialect-specific column types, dialect-specific server
defaults, foreign keys pointing outside the cloned set, and CHECK constraints
that reflection could not reproduce. Anything that has to be given up is
recorded rather than dropped silently -- see ``unsupported_constraints``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DefaultClause,
    ForeignKeyConstraint,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.types import TypeEngine

from safeagentdb.errors import SchemaError

# Keys the sandbox assigns for the agent start here, far above anything a real
# sequence will have issued. A key at or above this line is therefore known to
# be the sandbox's own placeholder rather than a value the agent supplied, which
# is what lets production assign the real one at commit time.
PROVISIONAL_KEY_BASE = 1 << 52

# ---- Type mapping for cross-dialect sandbox cloning ----

# SQLite doesn't support many Postgres/MySQL-specific types.
# We map them to safe SQLite-compatible equivalents for the sandbox only.
# The production sync uses the original metadata, so no fidelity is lost.

_SQLITE_TYPE_MAP: dict[str, type[TypeEngine]] = {
    # PostgreSQL
    "ARRAY": Text,
    "JSONB": Text,
    "JSON": Text,
    "UUID": String,
    "INET": String,
    "CIDR": String,
    "CITEXT": Text,
    "MACADDR": String,
    "HSTORE": Text,
    "TSVECTOR": Text,
    "BYTEA": Text,
    # MySQL
    "ENUM": String,
    "SET": String,
    "YEAR": String,
    "TINYINT": String,
}


def _sqlite_safe_type(col_type: TypeEngine) -> TypeEngine:
    """Convert a dialect-specific column type to a SQLite-compatible equivalent."""
    type_name = type(col_type).__name__.upper()

    mapped = _SQLITE_TYPE_MAP.get(type_name)
    if mapped is not None:
        if mapped is String:
            return String(length=255)
        return mapped()

    return col_type


# ---- Server default translation ----

# Postgres reflection returns defaults with explicit casts, e.g.
# "'free'::character varying". SQLite has no cast syntax, so strip it.
_CAST_RE = re.compile(r"::\s*[A-Za-z_][A-Za-z0-9_ ]*(\(\s*\d+\s*(,\s*\d+\s*)?\))?(\[\])*")

# Function-valued defaults SQLite cannot evaluate.
_UNREPRESENTABLE_DEFAULTS = (
    "nextval(",
    "currval(",
    "setval(",
    "gen_random_uuid(",
    "uuid_generate_v",
    "clock_timestamp(",
    "statement_timestamp(",
    "transaction_timestamp(",
)

_DEFAULT_REWRITES = {
    "now()": "CURRENT_TIMESTAMP",
    "current_timestamp": "CURRENT_TIMESTAMP",
    "current_date": "CURRENT_DATE",
    "current_time": "CURRENT_TIME",
    "true": "1",
    "false": "0",
}


def _sqlite_safe_server_default(sql_text: str) -> tuple[str | None, str | None]:
    """Translate a reflected server default into SQLite syntax.

    Returns ``(sqlite_sql, None)`` when the default can be reproduced, or
    ``(None, reason)`` when it cannot.
    """
    stripped = _CAST_RE.sub("", sql_text).strip()
    lowered = stripped.lower()

    if lowered in _DEFAULT_REWRITES:
        return _DEFAULT_REWRITES[lowered], None

    for pattern in _UNREPRESENTABLE_DEFAULTS:
        if pattern in lowered:
            return None, f"server default {sql_text!r} has no SQLite equivalent"

    if not stripped:
        return None, f"server default {sql_text!r} could not be translated"

    return stripped, None


def _is_balanced(expression: str) -> bool:
    """True when parentheses and quotes in a SQL fragment are balanced.

    SQLAlchemy reflects SQLite CHECK constraints with a regex that swallows the
    table's closing paren when it sits on the same line, yielding text like
    ``age >= 0)``. Re-emitting that produces invalid DDL, so it is caught here.
    """
    depth = 0
    in_string = False
    i = 0
    while i < len(expression):
        char = expression[i]
        if in_string:
            if char == "'":
                if i + 1 < len(expression) and expression[i + 1] == "'":
                    i += 1
                else:
                    in_string = False
        elif char == "'":
            in_string = True
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0 and not in_string


# ---- Sandbox table adaptation ----


def _adapt_column_types(table: Table, unsupported: list[str]) -> None:
    for col in table.columns:
        safe = _sqlite_safe_type(col.type)
        if safe is col.type:
            continue

        original_name = type(col.type).__name__.upper()
        col.type = safe
        unsupported.append(
            f"{table.name}.{col.name}: {original_name} stored as "
            f"{type(safe).__name__.upper()} in the sandbox; values valid here may "
            f"still be rejected by production"
        )


def _adapt_server_defaults(table: Table, unsupported: list[str]) -> list[str]:
    """Translate server defaults, returning the columns whose default was lost."""
    dropped: list[str] = []
    for col in table.columns:
        default = col.server_default
        if default is None:
            continue

        arg = getattr(default, "arg", None)
        if arg is None or isinstance(arg, str):
            # A plain string is a literal value, which SQLAlchemy quotes
            # correctly for SQLite. Only raw SQL needs translating.
            continue

        original = getattr(arg, "text", None)
        if original is None:
            continue

        translated, reason = _sqlite_safe_server_default(original)
        if translated is None:
            col.server_default = None
            dropped.append(col.name)
            unsupported.append(f"{table.name}.{col.name}: {reason}")
        else:
            col.server_default = DefaultClause(text(translated))

    return dropped


def _drop_unenforceable_foreign_keys(
    table: Table,
    known_tables: set[str],
    empty_tables: set[str],
    unsupported: list[str],
) -> None:
    """Remove foreign keys the sandbox cannot honestly enforce.

    Two cases, both of which would otherwise reject correct work:

    * the parent table was never cloned, so nothing could ever match;
    * the parent table cloned zero rows, because it carries no tenant column
      and was not listed in ``reference_tables``. Every reference from the
      child then looks dangling even though production has the parent row.
    """
    for constraint in list(table.constraints):
        if not isinstance(constraint, ForeignKeyConstraint):
            continue

        targets = {fk.target_fullname.rsplit(".", 1)[0] for fk in constraint.elements}
        columns = sorted(col.name for col in constraint.columns)

        missing = targets - known_tables
        if missing:
            _remove_foreign_key(table, constraint)
            unsupported.append(
                f"{table.name}: FOREIGN KEY ({', '.join(columns)}) -> "
                f"{', '.join(sorted(missing))} not enforced -- the parent table "
                f"is not part of the sandbox"
            )
            continue

        empty = targets & empty_tables
        if empty:
            _remove_foreign_key(table, constraint)
            unsupported.append(
                f"{table.name}: FOREIGN KEY ({', '.join(columns)}) -> "
                f"{', '.join(sorted(empty))} not enforced -- the parent table "
                f"cloned 0 rows, so every reference would look dangling. If "
                f"{', '.join(sorted(empty))} holds no tenant data, pass "
                f"reference_tables={sorted(empty)!r} to clone it in full."
            )


def _remove_foreign_key(table: Table, constraint: ForeignKeyConstraint) -> None:
    table.constraints.discard(constraint)
    for element in constraint.elements:
        table.foreign_keys.discard(element)
        for col in table.columns:
            col.foreign_keys.discard(element)


def _enable_provisional_key(table: Table, generated: list[str]) -> str | None:
    """Let the sandbox stand in for a production sequence on this table.

    Returns the key column the sandbox will fill provisionally, or None when the
    table does not qualify. Qualifying means the lost default belongs to a single
    integer primary key column -- exactly the ``serial``/identity shape.

    SQLite is switched to AUTOINCREMENT for the table so its counter persists in
    ``sqlite_sequence``, which ``seed_provisional_keys`` then moves above
    PROVISIONAL_KEY_BASE.
    """
    if len(generated) != 1:
        return None

    column_name = generated[0]
    pk_columns = [col.name for col in table.primary_key.columns]
    if pk_columns != [column_name]:
        return None

    column = table.c[column_name]
    if not isinstance(column.type, Integer):
        return None

    # SQLite only accepts AUTOINCREMENT on a column declared INTEGER; a BIGINT
    # would be rejected. SQLite integers are 64-bit either way, so nothing is
    # lost by narrowing the sandbox declaration.
    column.type = Integer()
    table.dialect_kwargs["sqlite_autoincrement"] = True
    return column_name


def seed_provisional_keys(
    sandbox_engine: Engine, sandbox_metadata: MetaData
) -> dict[str, list[str]]:
    """Move each provisional-key counter above PROVISIONAL_KEY_BASE.

    Called after the cloned rows are loaded, because loading real production ids
    advances the counter to their maximum. Returns the tables and columns that
    ended up usable; a table whose cloned rows already reach the sentinel is
    dropped, since its keys could no longer be told apart.
    """
    candidates: dict[str, list[str]] = dict(
        sandbox_metadata.info.get("provisional_key_columns", {})
    )
    if not candidates:
        return {}

    usable: dict[str, list[str]] = {}
    with sandbox_engine.connect() as conn:
        for table_name, columns in candidates.items():
            table = sandbox_metadata.tables[table_name]
            column = table.c[columns[0]]

            highest = conn.execute(select(func.max(column))).scalar()
            if highest is not None and highest >= PROVISIONAL_KEY_BASE:
                continue

            updated = conn.execute(
                text("UPDATE sqlite_sequence SET seq=:base WHERE name=:name"),
                {"base": PROVISIONAL_KEY_BASE, "name": table.name},
            ).rowcount
            if not updated:
                conn.execute(
                    text("INSERT INTO sqlite_sequence(name, seq) VALUES (:name, :base)"),
                    {"name": table.name, "base": PROVISIONAL_KEY_BASE},
                )
            usable[table_name] = list(columns)
        conn.commit()

    sandbox_metadata.info["provisional_key_columns"] = usable
    return usable


def _drop_malformed_checks(table: Table, unsupported: list[str]) -> None:
    for constraint in list(table.constraints):
        if not isinstance(constraint, CheckConstraint):
            continue

        expression = str(constraint.sqltext)
        if _is_balanced(expression):
            continue

        table.constraints.discard(constraint)
        unsupported.append(
            f"{table.name}.{constraint.name or 'check'}: CHECK ({expression}) "
            f"not enforced -- the reflected expression is malformed, a known "
            f"SQLAlchemy SQLite reflection limitation"
        )


def _strip_remaining_checks(table: Table, unsupported: list[str], reason: str) -> bool:
    """Last resort: drop CHECK constraints SQLite refused to accept."""
    removed = False
    for constraint in list(table.constraints):
        if isinstance(constraint, CheckConstraint):
            table.constraints.discard(constraint)
            unsupported.append(
                f"{table.name}.{constraint.name or 'check'}: CHECK "
                f"({constraint.sqltext}) not enforced -- SQLite rejected it ({reason})"
            )
            removed = True
    return removed


def _create_tables(
    metadata: MetaData, sandbox_engine: Engine, unsupported: list[str]
) -> None:
    for table in metadata.sorted_tables:
        try:
            table.create(bind=sandbox_engine)
            continue
        except OperationalError as exc:
            reason = str(exc.orig) if exc.orig is not None else str(exc)

        if not _strip_remaining_checks(table, unsupported, reason):
            raise SchemaError(
                f"Table '{table.name}' could not be reproduced in the SQLite "
                f"sandbox: {reason}"
            )

        try:
            table.create(bind=sandbox_engine)
        except OperationalError as exc:
            raise SchemaError(
                f"Table '{table.name}' could not be reproduced in the SQLite "
                f"sandbox: {exc.orig if exc.orig is not None else exc}"
            ) from exc


# ---- Public API ----


def create_sandbox_engine() -> Engine:
    """Create a fresh in-memory SQLite engine with foreign keys enforced."""
    engine = create_engine("sqlite:///:memory:", echo=False)

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def reflect_tables(
    source_engine: Engine,
    table_names: Sequence[str],
) -> MetaData:
    """Reflect specific table schemas from the production database."""
    metadata = MetaData()
    metadata.reflect(bind=source_engine, only=list(table_names))
    return metadata


def clone_schema_to_sandbox(
    source_metadata: MetaData,
    sandbox_engine: Engine,
    *,
    empty_tables: Sequence[str] | None = None,
) -> MetaData:
    """Recreate reflected table schemas in the sandbox engine.

    Constraints, unique indexes and column defaults are carried across for every
    dialect; only the parts SQLite genuinely cannot express are rewritten. The
    production metadata is never modified -- only the sandbox copy is adapted.

    Anything that had to be given up is recorded in
    ``metadata.info["unsupported_constraints"]`` and surfaced by
    ``ShadowDB.unsupported_constraints``. Columns whose production default could
    not be reproduced are recorded in ``metadata.info["generated_columns"]``.

    Args:
        empty_tables: Tables that will hold no rows in the sandbox. Foreign keys
            pointing at them are dropped, since nothing could ever satisfy them.

    Returns a new MetaData bound to the sandbox.
    """
    sandbox_metadata = MetaData()
    unsupported: list[str] = []
    generated: dict[str, list[str]] = {}
    provisional: dict[str, list[str]] = {}
    empty = set(empty_tables or ())

    # .tables rather than .sorted_tables: sorting resolves foreign keys, which
    # raises if a target table was not reflected. Sorting happens after the
    # external keys have been removed.
    known_tables = {table.name for table in source_metadata.tables.values()}
    for table in source_metadata.tables.values():
        sandbox_table = table.to_metadata(sandbox_metadata)
        _adapt_column_types(sandbox_table, unsupported)
        dropped = _adapt_server_defaults(sandbox_table, unsupported)
        if dropped:
            generated[sandbox_table.name] = dropped
            delegated = _enable_provisional_key(sandbox_table, dropped)
            if delegated:
                provisional[sandbox_table.name] = [delegated]
        _drop_unenforceable_foreign_keys(
            sandbox_table, known_tables, empty, unsupported
        )
        _drop_malformed_checks(sandbox_table, unsupported)

    _create_tables(sandbox_metadata, sandbox_engine, unsupported)

    sandbox_metadata.info["unsupported_constraints"] = unsupported
    sandbox_metadata.info["generated_columns"] = generated
    sandbox_metadata.info["provisional_key_columns"] = provisional
    return sandbox_metadata


def fetch_rows(
    source_engine: Engine,
    source_metadata: MetaData,
    tenant_column: str,
    tenant_id: Any,
    reference_tables: Sequence[str] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Read the rows that belong in the sandbox, without writing them yet.

    Reading first lets the caller see which tables will be empty before the
    sandbox schema is created, which decides whether a foreign key pointing at
    them can be enforced.

    A table listed in ``reference_tables`` is read in full, ignoring the tenant
    filter. A table with no tenant column that is not listed reads nothing.
    """
    references = set(reference_tables or ())
    fetched: dict[str, list[dict[str, Any]]] = {}

    with source_engine.connect() as src_conn:
        for table_key, src_table in source_metadata.tables.items():
            if table_key in references or src_table.name in references:
                stmt = select(src_table)
            elif tenant_column in src_table.c:
                stmt = select(src_table).where(src_table.c[tenant_column] == tenant_id)
            else:
                fetched[table_key] = []
                continue

            fetched[table_key] = [
                row._asdict() for row in src_conn.execute(stmt).fetchall()
            ]

    return fetched


def load_rows(
    sandbox_engine: Engine,
    sandbox_metadata: MetaData,
    fetched: dict[str, list[dict[str, Any]]],
) -> dict[str, int]:
    """Insert previously fetched rows into the sandbox.

    Foreign key enforcement is suspended for the duration of the load: a
    tenant-scoped clone is a partial view of production, so rows may legitimately
    reference parents that were never copied. Enforcement is restored afterwards,
    so changes the agent makes are checked.

    Returns a dict of {table_name: rows_loaded}.
    """
    stats: dict[str, int] = {}

    with sandbox_engine.connect() as sb_conn:
        sb_conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            # Parents before children, so the load order is valid on its own.
            for sb_table in sandbox_metadata.sorted_tables:
                rows = fetched.get(sb_table.key, [])
                if rows:
                    sb_conn.execute(insert(sb_table), rows)
                    sb_conn.commit()
                stats[sb_table.key] = len(rows)
        except Exception:
            sb_conn.rollback()
            raise
        finally:
            # PRAGMA is a no-op inside a transaction, so close any open one first.
            sb_conn.commit()
            sb_conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    return stats


def clone_rows(
    source_engine: Engine,
    sandbox_engine: Engine,
    source_metadata: MetaData,
    sandbox_metadata: MetaData,
    tenant_column: str,
    tenant_id: Any,
    reference_tables: Sequence[str] | None = None,
) -> dict[str, int]:
    """Fetch and load in one step. Returns {table_name: rows_copied}."""
    fetched = fetch_rows(
        source_engine, source_metadata, tenant_column, tenant_id, reference_tables
    )
    return load_rows(sandbox_engine, sandbox_metadata, fetched)


def _detect_dialect(metadata: MetaData) -> str:
    """Best-effort dialect detection from metadata's reflected tables.

    Informational only -- sandbox fidelity no longer depends on it.
    """
    for table in metadata.tables.values():
        for col in table.columns:
            type_module = type(col.type).__module__
            if "postgresql" in type_module:
                return "postgresql"
            if "mysql" in type_module:
                return "mysql"
    return "sqlite"
