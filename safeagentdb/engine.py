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

import datetime as dt
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
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
    type_coerce,
)
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.engine import Connection, Engine
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
}

# Types SQLite cannot name but whose values it holds exactly as they are, so
# they need a different declaration and nothing else -- no conversion, no
# entry in unsupported_constraints. A MySQL TINYINT is an integer; mapping it
# to String once handed a strict ``int`` validator the text '1'.
_SQLITE_PLAIN_TYPE_MAP: dict[str, type[TypeEngine]] = {
    "TINYINT": Integer,
    "MEDIUMINT": Integer,
}


# How a remapped column's values are carried through the sandbox. SQLite can
# only bind None, int, float, str and bytes, so a dict, list, UUID or IP address
# from the production driver has to become one of those on the way in and be
# turned back on the way out.
#
# JSON_CONVERSION: every JSON value is held as its JSON encoding, scalars
#     included, so the string "123", the number 123 and true stay distinct.
# DATETIME_CONVERSION: every datetime is held as ISO 8601 text, which keeps a
#     UTC offset that SQLite's own DATETIME storage would drop.
# TEXT_CONVERSION: anything else SQLite cannot name (UUID, INET, ...) is held
#     as its text form and handed back as text.
JSON_CONVERSION = "json"
DATETIME_CONVERSION = "datetime"
TEXT_CONVERSION = "text"

_JSON_TYPE_NAMES = frozenset({"JSON", "JSONB", "ARRAY", "HSTORE"})

_BINDABLE = (type(None), int, float, str, bytes)


class SandboxValueError(ValueError):
    """A sandbox value that cannot be turned back into its production type.

    Text in a JSON column that is not JSON, or text in a datetime column that
    is not a timestamp. The diff reports it as a failed row and the commit
    refuses it, rather than writing a guess.
    """


def _conversion_for(type_name: str) -> str:
    """Which conversion a remapped column needs."""
    return JSON_CONVERSION if type_name in _JSON_TYPE_NAMES else TEXT_CONVERSION


def to_sandbox_value(value: Any, conversion: str) -> Any:
    """Turn a production value into the form the sandbox holds it in."""
    if value is None:
        return None
    if conversion == JSON_CONVERSION:
        return json.dumps(value, sort_keys=True, default=str)
    if conversion == DATETIME_CONVERSION:
        # A SQLite production column is read as its stored text, which is kept
        # exactly; a datetime the driver parsed is written out in full.
        if isinstance(value, dt.datetime):
            return value.isoformat(sep=" ")
        return value
    if isinstance(value, _BINDABLE):
        return value
    return str(value)


# fromisoformat on Python 3.10 takes neither "Z", nor an offset without a
# colon, nor a fraction that is not 3 or 6 digits. This catches those.
_TIMESTAMP_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2}(?::\d{2})?)(?:\.(\d+))?)?"
    r"\s*(Z|[+-]\d{2}(?::?\d{2})?)?",
    re.IGNORECASE,
)


def parse_timestamp(value: str) -> dt.datetime:
    """Parse ISO 8601 text, keeping any UTC offset. Raises SandboxValueError."""
    candidate = value.strip()
    try:
        return dt.datetime.fromisoformat(candidate)
    except ValueError:
        pass

    match = _TIMESTAMP_RE.fullmatch(candidate)
    if match:
        day, clock, fraction, offset = match.groups()
        rebuilt = day
        if clock:
            rebuilt += " " + (clock if clock.count(":") == 2 else clock + ":00")
            if fraction:
                rebuilt += "." + fraction[:6].ljust(6, "0")
        if offset:
            if offset.upper() == "Z":
                offset = "+00:00"
            elif ":" not in offset:
                offset = offset[:3] + ":" + (offset[3:] or "00")
            rebuilt += offset
        try:
            return dt.datetime.fromisoformat(rebuilt)
        except ValueError:
            pass

    raise SandboxValueError(f"{value!r} is not a valid timestamp")


def decode_sandbox_value(value: Any, conversion: str) -> Any:
    """Turn a sandbox value back into the production representation, strictly.

    Raises SandboxValueError when the value cannot be read as its type. Nothing
    is guessed: text in a JSON column that is not JSON is an error, not a
    string.
    """
    if value is None:
        return None
    if conversion == JSON_CONVERSION:
        if isinstance(value, int | float) and not isinstance(value, bool):
            # SQLite stores a number written into the TEXT column as text, so
            # this only arises for a value that reached the row some other way.
            return value
        if not isinstance(value, str):
            raise SandboxValueError(f"{value!r} is not valid JSON")
        try:
            return json.loads(value)
        except ValueError:
            raise SandboxValueError(f"{value!r} is not valid JSON") from None
    if conversion == DATETIME_CONVERSION:
        if isinstance(value, dt.datetime):
            return value
        if isinstance(value, str):
            return parse_timestamp(value)
        raise SandboxValueError(f"{value!r} is not a valid timestamp")
    return value


def to_production_value(value: Any, conversion: str) -> Any:
    """Like decode_sandbox_value, but hands back a value it cannot read as is.

    For display and validation only. The commit checks every value it writes
    with decode_sandbox_value first, so nothing undecodable reaches production.
    """
    try:
        return decode_sandbox_value(value, conversion)
    except SandboxValueError:
        return value


def convert_rows_for_sandbox(
    rows: list[dict[str, Any]], conversions: dict[str, str]
) -> list[dict[str, Any]]:
    if not conversions:
        return rows
    return [
        {
            column: to_sandbox_value(value, conversions[column])
            if column in conversions
            else value
            for column, value in row.items()
        }
        for row in rows
    ]


def convert_row_for_production(
    row: dict[str, Any], conversions: dict[str, str]
) -> dict[str, Any]:
    if not conversions:
        return row
    return {
        column: to_production_value(value, conversions[column])
        if column in conversions
        else value
        for column, value in row.items()
    }


def _sqlite_safe_type(col_type: TypeEngine) -> TypeEngine:
    """Convert a dialect-specific column type to a SQLite-compatible equivalent."""
    type_name = type(col_type).__name__.upper()

    plain = _SQLITE_PLAIN_TYPE_MAP.get(type_name)
    if plain is not None:
        return plain()

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

    A reflected CHECK expression is re-emitted verbatim, so an unbalanced one
    would produce invalid DDL. SQLAlchemy 2.0.35 and earlier reflect SQLite
    CHECK constraints with a regex that swallows the table's closing paren when
    it sits on the same line, handing back text like ``age >= 0)``; later
    versions do not. This guard costs nothing when the expression is sound, so
    it stays either way -- reflection is not the only thing that can hand us an
    expression SQLite will not take.
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


def _adapt_column_types(table: Table, unsupported: list[str]) -> dict[str, str]:
    """Remap dialect types, returning {column: conversion} for the ones changed."""
    conversions: dict[str, str] = {}
    for col in table.columns:
        original_name = type(col.type).__name__.upper()
        if original_name in _SQLITE_PLAIN_TYPE_MAP:
            col.type = _sqlite_safe_type(col.type)
            continue

        if isinstance(col.type, DateTime):
            # Held as ISO 8601 text, which keeps the UTC offset. Lossless, so
            # nothing goes into unsupported_constraints.
            col.type = Text()
            conversions[col.name] = DATETIME_CONVERSION
            continue

        safe = _sqlite_safe_type(col.type)
        if safe is col.type:
            continue

        col.type = safe
        conversions[col.name] = _conversion_for(original_name)
        unsupported.append(
            f"{table.name}.{col.name}: {original_name} stored as "
            f"{type(safe).__name__.upper()} in the sandbox; values valid here may "
            f"still be rejected by production"
        )
    return conversions


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


def _apply_referential_actions(
    table: Table,
    actions: dict[tuple[str, ...], tuple[str | None, str | None]],
) -> None:
    """Give the sandbox's foreign keys the ON DELETE / ON UPDATE production has.

    SQLAlchemy's SQLite inspector leaves them out, so a sandbox cloned from a
    SQLite production refused deletes production accepts, and could not
    preview a cascade even when the child table was cloned.
    """
    for constraint in table.constraints:
        if not isinstance(constraint, ForeignKeyConstraint):
            continue
        columns = tuple(element.parent.name for element in constraint.elements)
        on_delete, on_update = actions.get(columns, (None, None))
        if constraint.ondelete is None and on_delete not in (None, "NO ACTION"):
            constraint.ondelete = on_delete
        if constraint.onupdate is None and on_update not in (None, "NO ACTION"):
            constraint.onupdate = on_update


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


# Referential actions that make the database write rows of its own accord.
_PROPAGATING_ACTIONS = ("CASCADE", "SET NULL", "SET DEFAULT")


@dataclass(frozen=True)
class CascadeReference:
    """A foreign key that makes production act beyond the statement we issue."""

    child_table: str
    child_columns: tuple[str, ...]
    parent_table: str
    parent_columns: tuple[str, ...]
    on_delete: str | None
    on_update: str | None
    child_in_sandbox: bool

    def describe(self) -> str:
        actions = []
        if self.on_delete:
            actions.append(f"ON DELETE {self.on_delete}")
        if self.on_update:
            actions.append(f"ON UPDATE {self.on_update}")
        scope = (
            "is part of the sandbox"
            if self.child_in_sandbox
            else "was never cloned, so the sandbox cannot see its rows at all"
        )
        return (
            f"{self.parent_table}: {self.child_table}"
            f"({', '.join(self.child_columns)}) references it "
            f"{' '.join(actions)} -- writing {self.parent_table} makes production "
            f"act on {self.child_table} by itself, outside any statement "
            f"SafeAgentDB issues. {self.child_table} {scope}. The commit refuses "
            f"a write that would make production act on any row there, unless "
            f"allow_cascades=True; with it, a row in another tenant or of unknown "
            f"owner is still refused."
        )


def referential_actions(
    source_engine: Engine, tables: Sequence[str]
) -> dict[str, dict[tuple[str, ...], tuple[str | None, str | None]]]:
    """{table: {constrained columns: (on_delete, on_update)}} for SQLite.

    Empty on every other dialect: their inspectors report the actions, so the
    reflected foreign keys already carry them into the sandbox.
    """
    if source_engine.dialect.name != "sqlite":
        return {}
    return {name: _sqlite_referential_actions(source_engine, name) for name in tables}


def find_cascade_references(
    source_engine: Engine,
    parent_tables: Sequence[str],
) -> list[CascadeReference]:
    """Foreign keys anywhere in production that propagate into a cloned table.

    Deliberately inspects EVERY table, not only the cloned ones: the dangerous
    case is a child table nobody asked to sandbox. Uses the inspector rather
    than full reflection, so this reads metadata and never builds Table objects
    for the whole schema.
    """
    parents = {name for name in parent_tables}
    if not parents:
        return []

    inspector = sa_inspect(source_engine)
    found: list[CascadeReference] = []

    for child in inspector.get_table_names():
        pragma_actions = _sqlite_referential_actions(source_engine, child)

        for fk in inspector.get_foreign_keys(child):
            referred = fk.get("referred_table")
            if referred not in parents:
                continue

            columns = tuple(fk.get("constrained_columns") or ())

            # PostgreSQL and MySQL report the actions here. SQLite's inspector
            # leaves options empty, so PRAGMA foreign_key_list fills the gap.
            options = fk.get("options") or {}
            on_delete = (options.get("ondelete") or "").upper() or None
            on_update = (options.get("onupdate") or "").upper() or None
            if on_delete is None and on_update is None:
                on_delete, on_update = pragma_actions.get(columns, (None, None))

            on_delete = on_delete if on_delete in _PROPAGATING_ACTIONS else None
            on_update = on_update if on_update in _PROPAGATING_ACTIONS else None
            if on_delete is None and on_update is None:
                continue

            found.append(
                CascadeReference(
                    child_table=child,
                    child_columns=columns,
                    parent_table=referred,
                    parent_columns=tuple(fk.get("referred_columns") or ()),
                    on_delete=on_delete,
                    on_update=on_update,
                    child_in_sandbox=child in parents,
                )
            )

    return sorted(found, key=lambda r: (r.parent_table, r.child_table, r.child_columns))


def _sqlite_referential_actions(
    source_engine: Engine, table: str
) -> dict[tuple[str, ...], tuple[str | None, str | None]]:
    """{constrained columns: (on_delete, on_update)} from SQLite's own pragma.

    SQLAlchemy's SQLite inspector returns an empty ``options`` for every
    foreign key, so the referential action is invisible there. The pragma
    reports it. Returns an empty mapping on any other dialect.
    """
    if source_engine.dialect.name != "sqlite":
        return {}

    grouped: dict[int, dict] = {}
    with source_engine.connect() as conn:
        rows = conn.exec_driver_sql(
            f'PRAGMA foreign_key_list("{table}")'
        ).fetchall()

    for row in rows:
        fields = ("id", "seq", "table", "from", "to", "on_update", "on_delete")
        entry = dict(zip(fields, row, strict=False))
        bucket = grouped.setdefault(entry["id"], {"columns": [], "actions": (None, None)})
        bucket["columns"].append(entry["from"])
        bucket["actions"] = (
            (entry["on_delete"] or "").upper() or None,
            (entry["on_update"] or "").upper() or None,
        )

    return {
        tuple(bucket["columns"]): bucket["actions"] for bucket in grouped.values()
    }


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
    actions: dict[str, dict[tuple[str, ...], tuple[str | None, str | None]]] | None = None,
) -> MetaData:
    """Recreate reflected table schemas in the sandbox engine.

    Constraints, unique indexes and column defaults are carried across for every
    dialect; only the parts SQLite genuinely cannot express are rewritten. The
    production metadata is never modified -- only the sandbox copy is adapted.

    Anything that had to be given up is recorded in
    ``metadata.info["unsupported_constraints"]`` and surfaced by
    ``ShadowDB.unsupported_constraints``. Columns whose production default could
    not be reproduced are recorded in ``metadata.info["generated_columns"]``,
    and datetime columns without a time zone in
    ``metadata.info["naive_datetime_columns"]``.

    Args:
        empty_tables: Tables that will hold no rows in the sandbox. Foreign keys
            pointing at them are dropped, since nothing could ever satisfy them.
        actions: Referential actions per table, from ``referential_actions()``,
            for a production whose reflection does not report them (SQLite).

    Returns a new MetaData bound to the sandbox.
    """
    sandbox_metadata = MetaData()
    unsupported: list[str] = []
    generated: dict[str, list[str]] = {}
    provisional: dict[str, list[str]] = {}
    converted: dict[str, dict[str, str]] = {}
    naive: dict[str, list[str]] = {}
    empty = set(empty_tables or ())

    # .tables rather than .sorted_tables: sorting resolves foreign keys, which
    # raises if a target table was not reflected. Sorting happens after the
    # external keys have been removed.
    known_tables = {table.name for table in source_metadata.tables.values()}
    for table in source_metadata.tables.values():
        sandbox_table = table.to_metadata(sandbox_metadata)
        without_zone = [
            col.name
            for col in table.columns
            if isinstance(col.type, DateTime) and not col.type.timezone
        ]
        if without_zone:
            naive[sandbox_table.name] = without_zone
        if actions and sandbox_table.name in actions:
            _apply_referential_actions(sandbox_table, actions[sandbox_table.name])
        conversions = _adapt_column_types(sandbox_table, unsupported)
        if conversions:
            converted[sandbox_table.name] = conversions
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
    sandbox_metadata.info["converted_columns"] = converted
    sandbox_metadata.info["naive_datetime_columns"] = naive
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
    raw_datetimes = source_engine.dialect.name == "sqlite"

    with source_engine.connect() as src_conn:
        for table_key, src_table in source_metadata.tables.items():
            columns = _readable_columns(src_table, raw_datetimes)
            if table_key in references or src_table.name in references:
                stmt = select(*columns)
            elif tenant_column in src_table.c:
                stmt = select(*columns).where(src_table.c[tenant_column] == tenant_id)
            else:
                fetched[table_key] = []
                continue

            fetched[table_key] = [
                row._asdict() for row in src_conn.execute(stmt).fetchall()
            ]

    return fetched


def _readable_columns(table: Table, raw_datetimes: bool) -> list[Any]:
    """The columns to read, with SQLite datetimes read as their stored text.

    SQLAlchemy's SQLite DATETIME parser drops a UTC offset, so
    '2024-01-01 10:00:00+02:00' and '2024-01-01T10:00:00Z' came back as the
    same naive time. The stored text is kept instead, exactly as it is.
    """
    if not raw_datetimes:
        return list(table.c)
    return [
        type_coerce(col, Text()).label(col.name) if isinstance(col.type, DateTime) else col
        for col in table.c
    ]


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
            conversions_by_table = sandbox_metadata.info.get("converted_columns", {})
            for sb_table in sandbox_metadata.sorted_tables:
                rows = fetched.get(sb_table.key, [])
                if rows:
                    sb_conn.execute(
                        insert(sb_table),
                        convert_rows_for_sandbox(
                            rows, conversions_by_table.get(sb_table.name, {})
                        ),
                    )
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


# ---- Change tracking inside the sandbox ----

# Prefix for the sandbox's own TEMP objects. They live in SQLite's temp schema,
# so they never appear in sqlite_master and never reach a diff.
_TRACKING_PREFIX = "_safeagentdb_"


@dataclass
class ChangeTracker:
    """What the sandbox records about how rows changed, not only what they hold.

    A diff compares two snapshots, so it cannot tell a row whose key the agent
    UPDATEd from a row it DELETEd beside one it INSERTed. Production can: the
    first runs ON UPDATE actions, the second ON DELETE ones. TEMP triggers
    record every key change as it happens, so the diff can refuse it.

    They also record a key the agent supplied itself in a column the sandbox
    fills provisionally, which is how a generated key is told apart from an
    explicit one without looking at its value.

    Attributes:
        key_logs: {table: (log table, row key columns)}.
        explicit_logs: {table: (log table, provisional key column)}.
        objects: {name: sql} of every TEMP object installed, so that an agent
            dropping or altering one is detected rather than trusted.
    """

    key_logs: dict[str, tuple[Table, list[str]]]
    explicit_logs: dict[str, tuple[Table, str]]
    objects: dict[str, str | None]


def install_change_tracking(
    sandbox_engine: Engine,
    sandbox_metadata: MetaData,
    row_keys: dict[str, list[str]],
    provisional_key_columns: dict[str, list[str]],
) -> ChangeTracker:
    """Install the TEMP triggers that feed a ChangeTracker.

    Called after the cloned rows are loaded, so loading is not recorded.
    """
    quote = sandbox_engine.dialect.identifier_preparer.quote
    log_metadata = MetaData()
    key_logs: dict[str, tuple[Table, list[str]]] = {}
    explicit_logs: dict[str, tuple[Table, str]] = {}

    with sandbox_engine.connect() as conn:
        for index, (table_name, key_columns) in enumerate(sorted(row_keys.items())):
            table = sandbox_metadata.tables[table_name]
            log = Table(
                f"{_TRACKING_PREFIX}keys_{index}",
                log_metadata,
                Column("seq", Integer, primary_key=True),
                *[Column(f"o{j}", table.c[c].type) for j, c in enumerate(key_columns)],
                *[Column(f"n{j}", table.c[c].type) for j, c in enumerate(key_columns)],
                Column("deleted", Integer),
                prefixes=["TEMPORARY"],
            )
            log.create(conn)
            changed = " OR ".join(f"OLD.{quote(c)} IS NOT NEW.{quote(c)}" for c in key_columns)
            targets = [f"o{j}" for j in range(len(key_columns))]
            targets += [f"n{j}" for j in range(len(key_columns))]
            values = [f"OLD.{quote(c)}" for c in key_columns]
            values += [f"NEW.{quote(c)}" for c in key_columns]
            conn.exec_driver_sql(
                f"CREATE TEMP TRIGGER {quote(log.name + '_t')} AFTER UPDATE ON "
                f"main.{quote(table.name)} WHEN {changed} BEGIN INSERT INTO "
                f"{quote(log.name)} ({', '.join(targets)}) VALUES "
                f"({', '.join(values)}); END"
            )
            # A deletion is logged too, so a key the agent frees and reuses is
            # not taken for the row that held it before.
            olds = [f"o{j}" for j in range(len(key_columns))]
            conn.exec_driver_sql(
                f"CREATE TEMP TRIGGER {quote(log.name + '_d')} AFTER DELETE ON "
                f"main.{quote(table.name)} BEGIN INSERT INTO {quote(log.name)} "
                f"({', '.join(olds)}, deleted) VALUES "
                f"({', '.join(f'OLD.{quote(c)}' for c in key_columns)}, 1); END"
            )
            _make_read_only(conn, quote, log.name)
            key_logs[table_name] = (log, list(key_columns))

        for index, (table_name, columns) in enumerate(sorted(provisional_key_columns.items())):
            table = sandbox_metadata.tables[table_name]
            column = columns[0]
            log = Table(
                f"{_TRACKING_PREFIX}explicit_{index}",
                log_metadata,
                Column("seq", Integer, primary_key=True),
                Column("k", table.c[column].type),
                prefixes=["TEMPORARY"],
            )
            log.create(conn)
            # SQLite gives NEW.<rowid alias> a negative value in a BEFORE INSERT
            # trigger when it is about to generate the key, so only a key the
            # agent wrote itself reaches the sentinel here. Were that ever not
            # so, every generated key would read as explicit and be refused --
            # the failure is closed, never a placeholder reaching production.
            conn.exec_driver_sql(
                f"CREATE TEMP TRIGGER {quote(log.name + '_t')} BEFORE INSERT ON "
                f"main.{quote(table.name)} WHEN NEW.{quote(column)} >= "
                f"{PROVISIONAL_KEY_BASE} BEGIN INSERT INTO {quote(log.name)} (k) "
                f"VALUES (NEW.{quote(column)}); END"
            )
            # A key the agent sets by UPDATE, on its own new row or any other,
            # is supplied just the same.
            conn.exec_driver_sql(
                f"CREATE TEMP TRIGGER {quote(log.name + '_u')} BEFORE UPDATE OF "
                f"{quote(column)} ON main.{quote(table.name)} WHEN "
                f"NEW.{quote(column)} >= {PROVISIONAL_KEY_BASE} AND "
                f"NEW.{quote(column)} IS NOT OLD.{quote(column)} BEGIN INSERT INTO "
                f"{quote(log.name)} (k) VALUES (NEW.{quote(column)}); END"
            )
            _make_read_only(conn, quote, log.name)
            explicit_logs[table_name] = (log, column)

        conn.commit()
        objects = _tracking_objects(conn)

    return ChangeTracker(key_logs=key_logs, explicit_logs=explicit_logs, objects=objects)


def _make_read_only(conn: Connection, quote: Any, log_name: str) -> None:
    for operation in ("UPDATE", "DELETE"):
        conn.exec_driver_sql(
            f"CREATE TEMP TRIGGER {quote(f'{log_name}_{operation.lower()}')} BEFORE "
            f"{operation} ON {quote(log_name)} BEGIN SELECT RAISE(ABORT, "
            f"'SafeAgentDB change tracking is read-only'); END"
        )


def _tracking_objects(conn: Connection) -> dict[str, str | None]:
    rows = conn.exec_driver_sql(
        "SELECT name, sql FROM sqlite_temp_master WHERE substr(name, 1, ?) = ?",
        (len(_TRACKING_PREFIX), _TRACKING_PREFIX),
    ).fetchall()
    return {name: sql for name, sql in rows}


def tracking_problems(sandbox_engine: Engine, tracker: ChangeTracker) -> list[str]:
    """TEMP tracking objects that were dropped or changed since they were made."""
    with sandbox_engine.connect() as conn:
        current = _tracking_objects(conn)
    return sorted(
        name for name, sql in tracker.objects.items() if current.get(name, object()) != sql
    )


def read_key_changes(
    sandbox_engine: Engine, tracker: ChangeTracker
) -> dict[str, list[tuple[tuple, tuple | None]]]:
    """{table: [(old key, new key), ...]} in the order the agent made them.

    A deleted row appears as (old key, None).
    """
    changes: dict[str, list[tuple[tuple, tuple | None]]] = {}
    with sandbox_engine.connect() as conn:
        for table_name, (log, key_columns) in tracker.key_logs.items():
            width = len(key_columns)
            rows = conn.execute(select(log).order_by(log.c.seq)).fetchall()
            if rows:
                changes[table_name] = [
                    (
                        tuple(row[1 : 1 + width]),
                        None if row.deleted else tuple(row[1 + width : 1 + 2 * width]),
                    )
                    for row in rows
                ]
    return changes


def read_explicit_keys(sandbox_engine: Engine, tracker: ChangeTracker) -> dict[str, set]:
    """{table: keys the agent supplied itself at or above the sentinel}."""
    found: dict[str, set] = {}
    with sandbox_engine.connect() as conn:
        for table_name, (log, _column) in tracker.explicit_logs.items():
            values = {row[0] for row in conn.execute(select(log.c.k))}
            if values:
                found[table_name] = values
    return found


def schema_signature(sandbox_engine: Engine) -> tuple[tuple, ...]:
    """Every object in the sandbox's main schema, to detect DDL by the agent.

    SQLite's statistics tables are left out: ANALYZE creates them, and they
    change no table a diff reads.
    """
    with sandbox_engine.connect() as conn:
        rows = conn.exec_driver_sql(
            "SELECT type, name, tbl_name, sql FROM sqlite_master"
            " WHERE name NOT IN ('sqlite_stat1', 'sqlite_stat4') ORDER BY type, name"
        ).fetchall()
    return tuple(tuple(row) for row in rows)


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
