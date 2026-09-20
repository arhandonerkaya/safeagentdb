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
from typing import Any, Sequence

from sqlalchemy import (
    CheckConstraint,
    DefaultClause,
    ForeignKeyConstraint,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    insert,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.types import TypeEngine

from safeagentdb.errors import SchemaError

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


def _adapt_server_defaults(table: Table, unsupported: list[str]) -> None:
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
            unsupported.append(f"{table.name}.{col.name}: {reason}")
        else:
            col.server_default = DefaultClause(text(translated))


def _drop_external_foreign_keys(
    table: Table, known_tables: set[str], unsupported: list[str]
) -> None:
    """Remove FKs whose target was not cloned.

    Keeping them would make the sandbox reject every insert into the child
    table once foreign_keys enforcement is on, and would break table sorting.
    """
    for constraint in list(table.constraints):
        if not isinstance(constraint, ForeignKeyConstraint):
            continue

        targets = {
            fk.target_fullname.rsplit(".", 1)[0] for fk in constraint.elements
        }
        if targets <= known_tables:
            continue

        _remove_foreign_key(table, constraint)
        unsupported.append(
            f"{table.name}: FOREIGN KEY -> {sorted(targets)} not enforced "
            f"(target table is not part of the sandbox)"
        )


def _remove_foreign_key(table: Table, constraint: ForeignKeyConstraint) -> None:
    table.constraints.discard(constraint)
    for element in constraint.elements:
        table.foreign_keys.discard(element)
        for col in table.columns:
            col.foreign_keys.discard(element)


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
) -> MetaData:
    """Recreate reflected table schemas in the sandbox engine.

    Constraints, unique indexes and column defaults are carried across for every
    dialect; only the parts SQLite genuinely cannot express are rewritten. The
    production metadata is never modified -- only the sandbox copy is adapted.

    Anything that had to be given up is recorded in
    ``metadata.info["unsupported_constraints"]`` and surfaced by
    ``ShadowDB.unsupported_constraints``.

    Returns a new MetaData bound to the sandbox.
    """
    sandbox_metadata = MetaData()
    unsupported: list[str] = []

    # .tables rather than .sorted_tables: sorting resolves foreign keys, which
    # raises if a target table was not reflected. Sorting happens after the
    # external keys have been removed.
    known_tables = {table.name for table in source_metadata.tables.values()}
    for table in source_metadata.tables.values():
        sandbox_table = table.to_metadata(sandbox_metadata)
        _adapt_column_types(sandbox_table, unsupported)
        _adapt_server_defaults(sandbox_table, unsupported)
        _drop_external_foreign_keys(sandbox_table, known_tables, unsupported)
        _drop_malformed_checks(sandbox_table, unsupported)

    _create_tables(sandbox_metadata, sandbox_engine, unsupported)

    sandbox_metadata.info["unsupported_constraints"] = unsupported
    return sandbox_metadata


def clone_rows(
    source_engine: Engine,
    sandbox_engine: Engine,
    source_metadata: MetaData,
    sandbox_metadata: MetaData,
    tenant_column: str,
    tenant_id: Any,
) -> dict[str, int]:
    """Copy tenant-scoped rows from production into the sandbox.

    Foreign key enforcement is suspended for the duration of the copy: a
    tenant-scoped clone is a partial view of production, so rows may legitimately
    reference parents that were never copied. Enforcement is restored afterwards,
    so changes the agent makes are checked.

    Returns a dict of {table_name: rows_copied}.
    """
    stats: dict[str, int] = {}

    with source_engine.connect() as src_conn, sandbox_engine.connect() as sb_conn:
        sb_conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        try:
            # Parents before children, so the load order is valid on its own.
            for sb_table in sandbox_metadata.sorted_tables:
                table_key = sb_table.key
                src_table = source_metadata.tables[table_key]

                if tenant_column not in src_table.c:
                    stats[table_key] = 0
                    continue

                rows = src_conn.execute(
                    select(src_table).where(
                        src_table.c[tenant_column] == tenant_id
                    )
                ).fetchall()

                if rows:
                    sb_conn.execute(
                        insert(sb_table),
                        [row._asdict() for row in rows],
                    )
                    sb_conn.commit()

                stats[table_key] = len(rows)
        except Exception:
            sb_conn.rollback()
            raise
        finally:
            # PRAGMA is a no-op inside a transaction, so close any open one first.
            sb_conn.commit()
            sb_conn.exec_driver_sql("PRAGMA foreign_keys=ON")

    return stats


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
