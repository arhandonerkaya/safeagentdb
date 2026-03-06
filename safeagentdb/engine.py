"""
engine.py - Manages production DB connections and in-memory sandbox creation.

Handles dialect-agnostic schema reflection and type mapping so that tables
from PostgreSQL, MySQL, or any SQLAlchemy-supported DB can be cloned into
an in-memory SQLite sandbox without errors.
"""

from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import (
    Column,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    insert,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.types import TypeEngine


# ---- Type mapping for cross-dialect sandbox cloning ----

# SQLite doesn't support many Postgres/MySQL-specific types.
# We map them to safe SQLite-compatible equivalents for the sandbox only.
# The production sync uses the original metadata, so no fidelity is lost.

_SQLITE_TYPE_MAP: dict[str, type[TypeEngine]] = {}

try:
    from sqlalchemy.dialects.postgresql import (
        ARRAY,
        BYTEA,
        CITEXT,
        HSTORE,
        INET,
        JSON as PG_JSON,
        JSONB,
        MACADDR,
        TSVECTOR,
        UUID as PG_UUID,
    )
    _SQLITE_TYPE_MAP.update({
        "ARRAY": Text,
        "JSONB": Text,
        "JSON": Text,
        "UUID": String,
        "INET": String,
        "CITEXT": Text,
        "MACADDR": String,
        "HSTORE": Text,
        "TSVECTOR": Text,
        "BYTEA": Text,
    })
except ImportError:
    pass

try:
    from sqlalchemy.dialects.mysql import (
        ENUM as MYSQL_ENUM,
        JSON as MYSQL_JSON,
        TINYINT,
        YEAR,
    )
    _SQLITE_TYPE_MAP.update({
        "ENUM": String,
        "YEAR": String,
        "TINYINT": String,
    })
except ImportError:
    pass


def _sqlite_safe_type(col_type: TypeEngine) -> TypeEngine:
    """Convert a dialect-specific column type to a SQLite-compatible equivalent."""
    type_name = type(col_type).__name__.upper()

    mapped = _SQLITE_TYPE_MAP.get(type_name)
    if mapped is not None:
        if mapped is String:
            return String(length=255)
        return mapped()

    return col_type


def _clone_table_for_sqlite(table: Table, target_metadata: MetaData) -> Table:
    """Clone a reflected Table into target metadata with SQLite-safe column types."""
    columns = []
    for col in table.columns:
        new_type = _sqlite_safe_type(col.type)
        columns.append(
            Column(
                col.name,
                new_type,
                primary_key=col.primary_key,
                nullable=col.nullable,
            )
        )

    return Table(table.name, target_metadata, *columns)


# ---- Public API ----


def create_sandbox_engine() -> Engine:
    """Create a fresh in-memory SQLite engine."""
    return create_engine("sqlite:///:memory:", echo=False)


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

    Uses dialect-aware type mapping to convert Postgres/MySQL types
    to SQLite-compatible equivalents. The production metadata is never
    modified -- only the sandbox copy is adapted.

    Returns a new MetaData bound to the sandbox.
    """
    sandbox_metadata = MetaData()

    source_dialect = _detect_dialect(source_metadata)
    needs_mapping = source_dialect != "sqlite"

    for table in source_metadata.sorted_tables:
        if needs_mapping:
            _clone_table_for_sqlite(table, sandbox_metadata)
        else:
            table.to_metadata(sandbox_metadata)

    sandbox_metadata.create_all(bind=sandbox_engine)
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

    Returns a dict of {table_name: rows_copied}.
    """
    stats: dict[str, int] = {}

    with source_engine.connect() as src_conn, sandbox_engine.connect() as sb_conn:
        for table_name, src_table in source_metadata.tables.items():
            if tenant_column not in src_table.c:
                stats[table_name] = 0
                continue

            rows = src_conn.execute(
                select(src_table).where(
                    src_table.c[tenant_column] == tenant_id
                )
            ).fetchall()

            if rows:
                sb_table = sandbox_metadata.tables[table_name]
                sb_conn.execute(
                    insert(sb_table),
                    [row._asdict() for row in rows],
                )
                sb_conn.commit()

            stats[table_name] = len(rows)

    return stats


def _detect_dialect(metadata: MetaData) -> str:
    """Best-effort dialect detection from metadata's reflected tables."""
    for table in metadata.sorted_tables:
        for col in table.columns:
            type_name = type(col.type).__module__
            if "postgresql" in type_name:
                return "postgresql"
            if "mysql" in type_name:
                return "mysql"
    return "sqlite"
