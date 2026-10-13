"""
diff.py - Row-level diff engine between sandbox state and original snapshot.

Computes INSERT / UPDATE / DELETE changesets per table and renders
a Rich-powered visual diff dashboard with per-row Pydantic validation.

Non-TTY safe: display() auto-detects whether stdout supports ANSI colors.
When piped to a file or CI log, it falls back to clean plain-text output.
"""

from __future__ import annotations

import datetime as dt
import decimal
import hashlib
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from enum import Enum
from io import StringIO
from typing import Any

from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from safeagentdb.engine import (
    DATETIME_CONVERSION,
    JSON_CONVERSION,
    PROVISIONAL_KEY_BASE,
    SandboxValueError,
    decode_sandbox_value,
    to_production_value,
)
from safeagentdb.errors import SchemaError
from safeagentdb.models import (
    format_validation_error,
    get_validator,
    missing_validator_message,
)

_BLOCKED_HEADER = "[BLOCKED] SAFETY ALERT -- INVALID DATA DETECTED"
_SAFE_HEADER = "[SAFE] AI CHANGES VERIFIED -- SAFE TO COMMIT"

# Two checks genuinely need production objects, so a clean diff cannot promise
# the commit will succeed. Saying so is the honest version of a green banner.
_COMMIT_CAVEAT = (
    "  note: the commit re-checks against production and can still refuse on "
    "drift since the clone, on a constraint whose other side is outside this "
    "tenant, or on a referential action that would act on rows not shown here."
)

_OP_STYLES = {
    "INSERT": "bold green",
    "UPDATE": "bold yellow",
    "DELETE": "bold red",
}

_OP_ICONS = {
    "INSERT": "+",
    "UPDATE": "~",
    "DELETE": "-",
}


class DiffType(Enum):
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


def canonical_value(value: Any) -> Any:
    """A JSON-serialisable form of ``value`` that keeps its type.

    Python's ``==`` makes 1, 1.0 and True equal, and ``json.dumps(default=str)``
    makes b'x' and the text "b'x'" identical. Both let a change go unseen --
    unwritten, or slipped past a reviewed fingerprint. Here every value is
    tagged with its type, so two values are only equal when they are the same
    value of the same type. Dict keys are sorted, so key order is not a change.
    """
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", value]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", repr(value)]
    if isinstance(value, decimal.Decimal):
        return ["decimal", str(value)]
    if isinstance(value, str):
        return ["str", value]
    if isinstance(value, bytes | bytearray | memoryview):
        return ["bytes", bytes(value).hex()]
    if isinstance(value, dt.datetime):
        return ["datetime", value.isoformat()]
    if isinstance(value, dt.date):
        return ["date", value.isoformat()]
    if isinstance(value, dt.time):
        return ["time", value.isoformat()]
    if isinstance(value, dt.timedelta):
        return ["timedelta", repr(value.total_seconds())]
    if isinstance(value, uuid.UUID):
        return ["uuid", str(value)]
    if isinstance(value, dict):
        items = [[canonical_value(k), canonical_value(v)] for k, v in value.items()]
        items.sort(key=lambda item: json.dumps(item[0], sort_keys=True))
        return ["dict", items]
    if isinstance(value, list | tuple):
        return ["list", [canonical_value(v) for v in value]]
    if isinstance(value, set | frozenset):
        members = sorted(json.dumps(canonical_value(v), sort_keys=True) for v in value)
        return ["set", members]
    return [f"{type(value).__module__}.{type(value).__qualname__}", repr(value)]


def _logical_canonical(value: Any, conversion: str | None) -> Any:
    """canonical_value of what production would hold, for change detection.

    A JSON value is compared by meaning, not by text, so '{"a": 1}' rewritten
    as '{"a":1}' is not a change, while '{"a": 1}' to '{"a": true}' is. A value
    that cannot be decoded is compared as itself, so it still shows as changed
    and the row is then refused for it.
    """
    if conversion is None:
        return canonical_value(value)
    try:
        return canonical_value(decode_sandbox_value(value, conversion))
    except SandboxValueError:
        return ["undecodable", canonical_value(value)]


def row_signature(row: dict[str, Any], converted: dict[str, str]) -> dict[str, Any]:
    """A row in canonical form, for deciding whether it changed."""
    return {col: _logical_canonical(value, converted.get(col)) for col, value in row.items()}


@dataclass
class RowDiff:
    """A single row-level change."""

    table: str
    diff_type: DiffType
    pk: dict[str, Any]
    old: dict[str, Any] | None = None
    new: dict[str, Any] | None = None
    require_validator: bool = True
    generated_columns: tuple[str, ...] = ()
    provisional_key_columns: tuple[str, ...] = ()
    converted_columns: dict[str, str] = field(default_factory=dict)
    # Keys the sandbox itself generated in this table's provisional key column.
    generated_keys: frozenset = frozenset()
    # {column: keys the sandbox generated in the table that column references},
    # for foreign-key columns pointing at a provisional key and nothing else.
    provisional_references: dict[str, frozenset] = field(default_factory=dict)
    # Every key the sandbox generated, in any table of the changeset.
    all_generated_keys: frozenset = frozenset()

    def logical_row(self, row: dict[str, Any] | None) -> dict[str, Any]:
        """A row as production sees it, not as the sandbox had to store it.

        A JSON or ARRAY column cannot be bound by SQLite, so the clone holds a
        serialised form. Validation and change detection must work on the
        production representation -- the SafeModel describes production, and a
        value that only differs in its serialisation has not changed.
        """
        if row is None:
            return {}
        if not self.converted_columns:
            return dict(row)
        return {
            column: to_production_value(value, self.converted_columns[column])
            if column in self.converted_columns
            else value
            for column, value in row.items()
        }

    def changed_columns(self) -> list[str]:
        """Columns whose value differs, by type as well as by value.

        Compared in canonical form, so 1 -> true in a JSON value is a change
        (Python's ``==`` called it equal and the write was dropped), while a
        JSON or timestamp value that is only formatted differently is not.
        """
        if self.diff_type != DiffType.UPDATE or not self.old or not self.new:
            return []
        old = row_signature(self.old, self.converted_columns)
        new = row_signature(self.new, self.converted_columns)
        return [k for k in new if k not in old or old[k] != new[k]]

    def written_columns(self) -> list[str]:
        """The columns the commit would write for this row."""
        if self.diff_type == DiffType.INSERT:
            return list(self.new or {})
        if self.diff_type == DiffType.UPDATE:
            return self.changed_columns()
        return []

    def undecodable_columns(self) -> dict[str, str]:
        """{column: reason} for written values production's type cannot take.

        Text in a JSON column that is not JSON, or text in a datetime column
        that is not a timestamp. Refused rather than written as a guess.
        """
        problems: dict[str, str] = {}
        row = self.new or {}
        for col in self.written_columns():
            conversion = self.converted_columns.get(col)
            if conversion not in (JSON_CONVERSION, DATETIME_CONVERSION):
                continue
            try:
                decode_sandbox_value(row.get(col), conversion)
            except SandboxValueError as exc:
                problems[col] = str(exc)
        return problems

    def undecodable_message(self, problems: dict[str, str]) -> str:
        details = "; ".join(f"{col}: {reason}" for col, reason in problems.items())
        hints = []
        if any("JSON" in reason for reason in problems.values()):
            hints.append(
                "a JSON column holds JSON text, so a string is written with its "
                "quotes, as '\"hello\"'"
            )
        if any("timestamp" in reason for reason in problems.values()):
            hints.append(
                "a datetime column holds ISO 8601 text, such as "
                "'2024-01-01 10:00:00' or '2024-01-01 10:00:00+02:00'"
            )
        return (
            f"{details} in '{self.table}'. SafeAgentDB will not guess what was "
            f"meant; {' and '.join(hints)}."
        )

    def pending_key_columns(self) -> list[str]:
        """Key columns this INSERT leaves for production to fill.

        The sandbox generated a placeholder in them. At commit they are left
        out of the statement so the production sequence assigns the real value.
        Only a key the sandbox is known to have generated counts -- never a
        value merely because it is large.
        """
        if self.diff_type != DiffType.INSERT:
            return []

        row_data = self.new or {}
        return [
            col
            for col in self.provisional_key_columns
            if row_data.get(col) in self.generated_keys
        ]

    def blocked_generated_columns(self) -> list[str]:
        """Columns on this INSERT whose value neither side can supply.

        The sandbox clone lost the production default (a sequence, an identity
        column, a uuid function). Where the column is a single integer primary
        key the sandbox holds it provisionally and production fills it at commit
        -- see ``pending_key_columns``. Everything else is blocked:

        * an explicit value in such a key column, because it bypasses the
          production sequence exactly as an invented one would;
        * a generated key the sandbox could not hold provisionally, so no side
          can produce a safe value;
        * a generated non-key column left NULL, since the default that would
          have filled it is gone.

        Shared by RowDiff.validate() and sync.apply_changeset so the dashboard
        and the commit can never disagree.
        """
        if self.diff_type != DiffType.INSERT or not self.generated_columns:
            return []

        row_data = self.new or {}
        blocked = []
        for col in self.generated_columns:
            value = row_data.get(col)
            if col in self.provisional_key_columns:
                if value not in self.generated_keys:
                    blocked.append(col)
            elif col in self.pk or value is None:
                blocked.append(col)
        return blocked

    def regenerated_key_columns(self) -> list[str]:
        """Provisional key columns an UPDATE would overwrite with a placeholder.

        Deleting a row and inserting it again under the same custom row_key
        reads as one UPDATE, whose generated key is the sandbox's placeholder.
        Writing it would put a meaningless id into production.
        """
        if self.diff_type != DiffType.UPDATE:
            return []
        row_data = self.new or {}
        changed = set(self.changed_columns())
        return [
            col
            for col in self.provisional_key_columns
            if col in changed and row_data.get(col) in self.generated_keys
        ]

    def regenerated_key_message(self, columns: list[str]) -> str:
        return (
            f"column(s) {', '.join(columns)} in '{self.table}' would be set to a "
            f"key the sandbox generated: the row was deleted and re-inserted under "
            f"the same row key, so it reads as an UPDATE carrying a new "
            f"placeholder. That key is assigned by production, and SafeAgentDB will not "
            f"write the placeholder as a real one. Update the existing row instead "
            f"of deleting and re-inserting it."
        )

    def provisional_reference_columns(self) -> list[str]:
        """Columns pointing at a key production has not assigned yet.

        A row referencing a row that is itself new would carry the sandbox's
        placeholder into production, where it means nothing. Decided by
        membership, never by size, so an ordinary large integer is just a value:

        * a foreign-key column onto a provisional key, holding a key the
          sandbox generated in that table;
        * any other column holding exactly a key the sandbox generated in any
          table. A schema need not declare the reference, and a placeholder
          copied anywhere is still meaningless in production.
        """
        if self.diff_type == DiffType.DELETE or not self.all_generated_keys:
            return []

        row_data = self.new or {}
        exempt = set(self.pending_key_columns()) | set(self.provisional_key_columns)
        found = []
        for col, value in row_data.items():
            if col in exempt or isinstance(value, bool):
                continue
            generated = self.provisional_references.get(col, self.all_generated_keys)
            if value in generated:
                found.append(col)
        return sorted(found)

    def generated_value_message(self, columns: list[str]) -> str:
        supplied = [
            col for col in columns if col in self.provisional_key_columns
        ]
        if supplied:
            return (
                f"column(s) {', '.join(supplied)} in '{self.table}' are assigned by "
                f"production (a sequence or identity column), but this row supplies "
                f"a value. That would bypass the sequence and can collide with a "
                f"future row. Leave the column out and production will fill it."
            )
        return (
            f"column(s) {', '.join(columns)} in '{self.table}' are generated by "
            f"production (a sequence, identity or default function). The sandbox "
            f"cannot produce the value production would, so this INSERT cannot be "
            f"synced. Insert rows for this table through the application instead."
        )

    def provisional_reference_message(self, columns: list[str]) -> str:
        return (
            f"column(s) {', '.join(columns)} in '{self.table}' reference a key that "
            f"production has not assigned yet -- the row they point at is itself new "
            f"in this changeset, and its real key is only known once it is written. "
            f"SafeAgentDB will not guess it. Insert the parent row through the "
            f"application first, then let the agent reference its real key."
        )

    def validate(self) -> tuple[bool, str]:
        """Report whether this row would be accepted at commit time.

        A missing validator, and a value only production can generate, are
        treated exactly as sync treats them, so the diff dashboard can never
        show a green light for a changeset the commit will refuse.
        """
        if self.diff_type == DiffType.DELETE:
            return True, "OK (delete)"

        row_data = self.new
        if row_data is None:
            return False, "Missing row data"

        undecodable = self.undecodable_columns()
        if undecodable:
            return False, self.undecodable_message(undecodable)

        blocked = self.blocked_generated_columns()
        if blocked:
            return False, self.generated_value_message(blocked)

        regenerated = self.regenerated_key_columns()
        if regenerated:
            return False, self.regenerated_key_message(regenerated)

        dangling = self.provisional_reference_columns()
        if dangling:
            return False, self.provisional_reference_message(dangling)

        validator_cls = get_validator(self.table)
        if validator_cls is None:
            if self.require_validator:
                return False, missing_validator_message(self.table)
            return True, "WARNING: no validator, row unchecked"

        try:
            validator_cls.model_validate(self.logical_row(row_data))
            return True, "OK"
        except ValidationError as e:
            return False, format_validation_error(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"


@dataclass
class ChangeSet:
    """Full set of diffs across all tables."""

    diffs: list[RowDiff] = field(default_factory=list)
    unsupported_constraints: list[str] = field(default_factory=list)
    blocking_errors: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return len(self.diffs) == 0

    @property
    def summary(self) -> dict[str, int]:
        counts = {"INSERT": 0, "UPDATE": 0, "DELETE": 0}
        for d in self.diffs:
            counts[d.diff_type.value] += 1
        return counts

    @property
    def fingerprint(self) -> str:
        """A stable hash of exactly what this changeset would write.

        Content-addressed and order-independent: two changesets with the same
        rows produce the same value whatever order the diffs are in, and any
        difference in table, operation, key or value changes it. Used by
        commit_to_production(changeset=...) to refuse writing something other
        than what was reviewed.
        """
        # Every value goes through canonical_value, which tags it with its type,
        # so b'x' and the text "b'x'" -- once identical under default=str --
        # can never produce the same entry.
        entries = sorted(
            json.dumps(
                {
                    "table": d.table,
                    "op": d.diff_type.value,
                    "key": canonical_value(d.pk),
                    "old": None if d.old is None else row_signature(d.old, d.converted_columns),
                    "new": None if d.new is None else row_signature(d.new, d.converted_columns),
                },
                sort_keys=True,
            )
            for d in self.diffs
        )
        digest = hashlib.sha256()
        for entry in entries:
            digest.update(entry.encode("utf-8"))
            digest.update(b"\x00")
        return digest.hexdigest()[:32]

    def validate_all(self) -> list[tuple[RowDiff, bool, str]]:
        return [(d, *d.validate()) for d in self.diffs]

    @property
    def is_valid(self) -> bool:
        """True when nothing SafeAgentDB can check without production objects.

        Covers per-row validation and every blocking error raised by the
        pre-flight checks. A changeset that is valid here can still be refused
        at commit by the checks that genuinely need production -- drift and
        cross-tenant uniqueness -- which is why the banner says so.
        """
        if self.blocking_errors:
            return False
        return all(valid for _, valid, _ in self.validate_all())

    def display(self) -> str:
        """Render a diff dashboard as a printable string.

        Auto-detects the output environment:
        - TTY (interactive terminal): returns Rich-formatted ANSI string with colors.
        - Non-TTY (pipe, file, CI log): returns clean plain-text table, no escapes.
        """
        is_tty = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

        if is_tty:
            buf = StringIO()
            console = Console(file=buf, force_terminal=True, width=120)
            self._render_rich(console)
            return buf.getvalue()

        return self._render_plain()

    def print(self) -> None:
        """Print the Rich diff dashboard directly to the terminal."""
        console = Console()
        self._render_rich(console)

    # ---- Rich rendering (TTY) ----

    def _render_rich(self, console: Console) -> None:
        if self.blocking_errors and self.is_empty:
            console.print()
            console.print(
                Panel(
                    f"[bold red]{_BLOCKED_HEADER}[/bold red]",
                    title="[bold red]BLOCKED[/bold red]",
                    border_style="red",
                    padding=(0, 2),
                )
            )
            self._render_rich_blocking(console)
            self._render_rich_unsupported(console)
            return

        if self.is_empty:
            console.print(
                Panel(
                    "[dim]No changes detected.[/dim]",
                    title="SafeAgentDB",
                    border_style="dim",
                )
            )
            self._render_rich_unsupported(console)
            return

        all_valid = self.is_valid
        s = self.summary

        if all_valid:
            banner = Panel(
                f"[bold green]{_SAFE_HEADER}[/bold green]\n"
                f"[dim]{_COMMIT_CAVEAT.strip()}[/dim]",
                title="[bold green]SAFE[/bold green]",
                border_style="green",
                padding=(0, 2),
            )
        else:
            banner = Panel(
                "[bold red][BLOCKED] SAFETY ALERT -- INVALID DATA DETECTED[/bold red]",
                title="[bold red]BLOCKED[/bold red]",
                border_style="red",
                padding=(0, 2),
            )

        console.print()
        console.print(banner)
        self._render_rich_blocking(console)
        self._render_rich_unsupported(console)

        stats_text = Text()
        stats_text.append("  ")
        if s["INSERT"]:
            stats_text.append(f"+{s['INSERT']} insert  ", style="green")
        if s["UPDATE"]:
            stats_text.append(f"~{s['UPDATE']} update  ", style="yellow")
        if s["DELETE"]:
            stats_text.append(f"-{s['DELETE']} delete  ", style="red")
        console.print(stats_text)
        console.print()

        table = Table(
            title="Row-Level Changes",
            title_style="bold",
            show_lines=False,
            pad_edge=True,
            expand=False,
            border_style="bright_black",
        )

        table.add_column("", style="dim", width=3, justify="center")
        table.add_column("Table", style="cyan", no_wrap=True)
        table.add_column("Op", no_wrap=True)
        table.add_column("PK", style="bright_white", no_wrap=True)
        table.add_column("Column", style="white", no_wrap=True)
        table.add_column("Old Value", no_wrap=True)
        table.add_column("New Value", no_wrap=True)
        table.add_column("Validation", justify="center", no_wrap=True)

        for d in self.diffs:
            is_valid, valid_msg = d.validate()
            op_style = _OP_STYLES[d.diff_type.value]
            icon = _OP_ICONS[d.diff_type.value]
            pk_str = _format_row_key(d)
            op_label = Text(d.diff_type.value, style=op_style)

            if is_valid:
                badge = Text("[PASS]", style="bold green")
            else:
                badge = Text("[FAIL]", style="bold red")

            col_rows = _build_column_rows(d)

            first = True
            for col_name, old_cell, new_cell in col_rows:
                table.add_row(
                    Text(icon, style=op_style) if first else Text(""),
                    Text(d.table, style="cyan") if first else Text(""),
                    op_label if first else Text(""),
                    Text(pk_str, style="bright_white") if first else Text(""),
                    Text(col_name, style="white"),
                    old_cell,
                    new_cell,
                    badge if first else Text(""),
                )
                first = False

            table.add_row(
                Text(""), Text(""), Text(""), Text(""),
                Text(""), Text(""), Text(""), Text(""),
                end_section=True,
            )

        console.print(table)

        if not all_valid:
            console.print()
            for d, valid, msg in self.validate_all():
                if not valid:
                    pk_str = _format_row_key(d)
                    console.print(
                        f"  [bold red]>[/bold red] [cyan]{d.table}[/cyan] "
                        f"pk={pk_str}: [red]{msg}[/red]"
                    )
            console.print()

    def _render_rich_blocking(self, console: Console) -> None:
        if not self.blocking_errors:
            return
        body = "\n".join(f"- {item}" for item in self.blocking_errors)
        console.print(
            Panel(
                f"[red]{body}[/red]",
                title="[bold red]BLOCKED BEFORE COMMIT[/bold red]",
                border_style="red",
                padding=(0, 2),
            )
        )

    def _render_rich_unsupported(self, console: Console) -> None:
        if not self.unsupported_constraints:
            return
        body = "\n".join(f"- {item}" for item in self.unsupported_constraints)
        console.print(
            Panel(
                f"[yellow]{body}[/yellow]\n\n"
                "[dim]These are NOT enforced in the sandbox. A violation will "
                "only surface when production rejects the commit.[/dim]",
                title="[bold yellow]NOT ENFORCED IN SANDBOX[/bold yellow]",
                border_style="yellow",
                padding=(0, 2),
            )
        )

    # ---- Plain-text rendering (non-TTY / logs / CI) ----

    def _render_plain(self) -> str:
        if self.blocking_errors and self.is_empty:
            return "\n".join(
                [_BLOCKED_HEADER]
                + self._plain_blocking_lines()
                + self._plain_unsupported_lines()
            )
        if self.is_empty:
            return "\n".join(
                ["SafeAgentDB: No changes detected."] + self._plain_unsupported_lines()
            )

        all_valid = self.is_valid
        s = self.summary

        if all_valid:
            header = _SAFE_HEADER
        else:
            header = _BLOCKED_HEADER

        stats_parts = []
        if s["INSERT"]:
            stats_parts.append(f"+{s['INSERT']} insert")
        if s["UPDATE"]:
            stats_parts.append(f"~{s['UPDATE']} update")
        if s["DELETE"]:
            stats_parts.append(f"-{s['DELETE']} delete")

        display_rows: list[tuple[str, str, str, str, str, str, str]] = []

        for d in self.diffs:
            is_valid, valid_msg = d.validate()
            status = "PASS" if is_valid else f"FAIL: {valid_msg}"
            pk_str = _format_row_key(d)
            op = d.diff_type.value

            if d.diff_type == DiffType.INSERT and d.new:
                for col, val in d.new.items():
                    display_rows.append(
                        (d.table, op, pk_str, col, "--", _display_value(d, col, val), status)
                    )
                    op, pk_str, status = "", "", ""

            elif d.diff_type == DiffType.DELETE and d.old:
                for col, val in d.old.items():
                    display_rows.append((d.table, op, pk_str, col, str(val), "--", status))
                    op, pk_str, status = "", "", ""

            elif d.diff_type == DiffType.UPDATE and d.old and d.new:
                for col in d.changed_columns():
                    display_rows.append((
                        d.table, op, pk_str,
                        col, str(d.old.get(col)), str(d.new[col]),
                        status,
                    ))
                    op, pk_str, status = "", "", ""

        headers = ("Table", "Op", "PK", "Column", "Old", "New", "Validation")
        widths = [len(h) for h in headers]
        for row in display_rows:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], min(len(cell), 40))

        def fmt(cells):
            return " | ".join(c[:widths[i]].ljust(widths[i]) for i, c in enumerate(cells))

        sep = "-+-".join("-" * w for w in widths)
        verdict = "ALL VALIDATIONS PASSED" if all_valid else "VALIDATION FAILURES DETECTED"

        lines = [header]
        if all_valid:
            lines.append(_COMMIT_CAVEAT)
        lines.extend(self._plain_blocking_lines())
        lines.extend(self._plain_unsupported_lines())
        lines.extend([
            "  " + "  ".join(stats_parts),
            "",
            fmt(headers),
            sep,
        ])
        for row in display_rows:
            lines.append(fmt(row))
        lines.append(sep)
        lines.append(f"  >> {verdict}")

        return "\n".join(lines)

    def _plain_blocking_lines(self) -> list[str]:
        if not self.blocking_errors:
            return []
        lines = ["[BLOCKED BEFORE COMMIT] these must be resolved first:"]
        lines.extend(f"  - {item}" for item in self.blocking_errors)
        lines.append("")
        return lines

    def _plain_unsupported_lines(self) -> list[str]:
        if not self.unsupported_constraints:
            return []
        lines = ["[NOT ENFORCED IN SANDBOX] violations surface only in production:"]
        lines.extend(f"  - {item}" for item in self.unsupported_constraints)
        lines.append("")
        return lines


def tenant_breach(
    diff: RowDiff, tenant_column: str, tenant_id: Any
) -> str | None:
    """The tenant rule, in one place.

    Called by sync.apply_changeset before it writes, and by ShadowDB.diff() so
    the dashboard reaches the same verdict. A breach used to be invisible until
    the commit raised.
    """
    if diff.diff_type in (DiffType.INSERT, DiffType.UPDATE):
        row_data = diff.new
        if row_data is None:
            return (
                f"Missing row data for {diff.diff_type.value} on '{diff.table}'."
            )
        found = row_data.get(tenant_column)
        if not same_tenant(found, tenant_id):
            return (
                f"Tenant breach blocked on {diff.diff_type.value}: row in "
                f"'{diff.table}' has {tenant_column}={found!r}, expected "
                f"{tenant_id!r}."
            )
        return None

    if diff.old and not same_tenant(diff.old.get(tenant_column), tenant_id):
        return (
            f"Tenant breach blocked on DELETE: row in '{diff.table}' belongs "
            f"to {tenant_column}={diff.old.get(tenant_column)!r}, not "
            f"{tenant_id!r}."
        )
    return None


_INTEGER_TEXT = re.compile(r"[+-]?\d+")


def normalise_tenant_id(value: Any, python_type: type) -> Any:
    """``value`` as the tenant column's Python type, converted exactly or not at all.

    A tenant id taken from a URL or a header is a string, while the column is
    usually an integer. Comparing '42' with 42 in Python made every write a
    "tenant breach". Raises ValueError when the conversion would not be exact,
    so nothing is guessed: '42' becomes 42, '42.0' and 'forty-two' do not.
    """
    if isinstance(value, python_type) and not (
        python_type is int and isinstance(value, bool)
    ):
        return value
    if python_type is int and isinstance(value, str) and _INTEGER_TEXT.fullmatch(value.strip()):
        return int(value.strip())
    if python_type is str and isinstance(value, int | uuid.UUID) and not isinstance(value, bool):
        return str(value)
    if python_type is uuid.UUID and isinstance(value, str):
        return uuid.UUID(value)
    raise ValueError(
        f"{value!r} ({type(value).__name__}) does not convert exactly to {python_type.__name__}"
    )


def same_tenant(value: Any, tenant_id: Any) -> bool:
    """Whether a row's tenant value is the acting tenant, compared normalised."""
    if value is None or tenant_id is None:
        return False
    try:
        return normalise_tenant_id(value, type(tenant_id)) == tenant_id
    except (TypeError, ValueError):
        return False


def _display_value(d: RowDiff, column: str, value: Any) -> str:
    if column in d.pending_key_columns():
        return "(assigned by production)"
    return str(value)


def _build_column_rows(d: RowDiff) -> list[tuple[str, Text, Text]]:
    rows: list[tuple[str, Text, Text]] = []

    if d.diff_type == DiffType.INSERT and d.new:
        for col, val in d.new.items():
            rows.append((
                col,
                Text("--", style="dim"),
                Text(_display_value(d, col, val), style="green"),
            ))

    elif d.diff_type == DiffType.DELETE and d.old:
        for col, val in d.old.items():
            rows.append((
                col,
                Text(str(val), style="red dim strikethrough"),
                Text("--", style="dim"),
            ))

    elif d.diff_type == DiffType.UPDATE and d.old and d.new:
        for col in d.changed_columns():
            old_val = str(d.old.get(col, ""))
            new_val = str(d.new[col])
            rows.append((
                col,
                Text(old_val, style="red"),
                Text(new_val, style="bold green"),
            ))

    return rows


def compute_diff(
    original_snapshot: dict[str, list[dict[str, Any]]],
    current_snapshot: dict[str, list[dict[str, Any]]],
    pk_columns: dict[str, list[str]],
    unsupported_constraints: list[str] | None = None,
    require_validators: bool = True,
    generated_columns: dict[str, list[str]] | None = None,
    provisional_key_columns: dict[str, list[str]] | None = None,
    converted_columns: dict[str, dict[str, str]] | None = None,
    explicit_keys: dict[str, set] | None = None,
    provisional_references: dict[str, dict[str, str]] | None = None,
) -> ChangeSet:
    """Diff two snapshots, row by row, keyed on each table's row key.

    Args:
        explicit_keys: {table: keys the agent wrote itself into a provisional
            key column}. Everything else in that column at or above the
            sentinel is a key the sandbox generated.
        provisional_references: {table: {column: referenced table}} for
            foreign-key columns pointing at a provisional key column.
    """
    changeset = ChangeSet(unsupported_constraints=list(unsupported_constraints or []))
    generated = generated_columns or {}
    provisional = provisional_key_columns or {}
    converted = converted_columns or {}
    explicit = explicit_keys or {}
    references = provisional_references or {}

    # The keys the sandbox generated, per provisional table. Cloned rows are
    # all below the sentinel (seed_provisional_keys guarantees it), so a value
    # at or above it was produced in the sandbox -- by the sandbox's counter,
    # unless the agent wrote it itself, which the explicit-key trigger saw.
    generated_keys: dict[str, frozenset] = {}
    for table_name, columns in provisional.items():
        column = columns[0]
        generated_keys[table_name] = frozenset(
            row[column]
            for row in current_snapshot.get(table_name, [])
            if isinstance(row.get(column), int)
            and not isinstance(row.get(column), bool)
            and row[column] >= PROVISIONAL_KEY_BASE
            and row[column] not in explicit.get(table_name, ())
        )

    all_generated = frozenset().union(*generated_keys.values())

    for table_name in sorted(set(original_snapshot) | set(current_snapshot)):
        pks = pk_columns.get(table_name, ["id"])
        table_generated = tuple(generated.get(table_name, ()))
        table_provisional = tuple(provisional.get(table_name, ()))
        table_converted = dict(converted.get(table_name, {}))
        own_generated = generated_keys.get(table_name, frozenset())
        table_references = {
            column: generated_keys.get(parent, frozenset())
            for column, parent in references.get(table_name, {}).items()
        }
        orig_rows = _keyed_rows(
            original_snapshot.get(table_name, []), pks, table_name, "cloned"
        )
        curr_rows = _keyed_rows(
            current_snapshot.get(table_name, []), pks, table_name, "current sandbox"
        )

        for pk_key in sorted(orig_rows.keys() - curr_rows.keys()):
            changeset.diffs.append(
                RowDiff(
                    table=table_name,
                    diff_type=DiffType.DELETE,
                    require_validator=require_validators,
                    converted_columns=table_converted,
                    pk=dict(zip(pks, pk_key, strict=False)),
                    old=orig_rows[pk_key],
                )
            )

        for pk_key in sorted(curr_rows.keys() - orig_rows.keys()):
            changeset.diffs.append(
                RowDiff(
                    table=table_name,
                    diff_type=DiffType.INSERT,
                    require_validator=require_validators,
                    generated_columns=table_generated,
                    provisional_key_columns=table_provisional,
                    converted_columns=table_converted,
                    generated_keys=own_generated,
                    provisional_references=table_references,
                    all_generated_keys=all_generated,
                    pk=dict(zip(pks, pk_key, strict=False)),
                    new=curr_rows[pk_key],
                )
            )

        for pk_key in sorted(orig_rows.keys() & curr_rows.keys()):
            # Compared by type and meaning, not with ==: 1 -> true is a change,
            # '{"a": 1}' -> '{"a":1}' is not.
            before = row_signature(orig_rows[pk_key], table_converted)
            after = row_signature(curr_rows[pk_key], table_converted)
            if before != after:
                changeset.diffs.append(
                    RowDiff(
                        table=table_name,
                        diff_type=DiffType.UPDATE,
                        pk=dict(zip(pks, pk_key, strict=False)),
                        old=orig_rows[pk_key],
                        new=curr_rows[pk_key],
                        require_validator=require_validators,
                        converted_columns=table_converted,
                        provisional_key_columns=table_provisional,
                        generated_keys=own_generated,
                        provisional_references=table_references,
                        all_generated_keys=all_generated,
                    )
                )

    return changeset


def _pk_key(row: dict[str, Any], pk_cols: list[str]) -> tuple:
    return tuple(row[c] for c in pk_cols)


def _keyed_rows(
    rows: list[dict[str, Any]], key_columns: list[str], table: str, side: str
) -> dict[tuple, dict[str, Any]]:
    """Index rows by their row key, refusing to let two rows share one.

    A dict comprehension would quietly keep the last of a colliding pair. That
    is how a second row sharing a custom row_key turned into an UPDATE that
    overwrote the first, so this raises instead of choosing a winner.
    """
    keyed: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = _pk_key(row, key_columns)
        if key in keyed:
            raise SchemaError(
                f"Row key {key_columns!r} is not unique in the {side} state of "
                f"table '{table}': the value {key!r} appears more than once, so "
                f"it cannot identify a row. Choose columns that uniquely "
                f"identify a row, or remove the duplicate."
            )
        keyed[key] = row
    return keyed


def _format_row_key(d: RowDiff) -> str:
    """Render a row key, hiding placeholders the sandbox has not had filled yet."""
    pending = set(d.pending_key_columns())
    if not pending:
        return _format_pk(d.pk)
    return _format_pk(
        {k: ("pending" if k in pending else v) for k, v in d.pk.items()}
    )


def _format_pk(pk: dict[str, Any]) -> str:
    if len(pk) == 1:
        return str(next(iter(pk.values())))
    return ",".join(f"{k}={v}" for k, v in pk.items())
