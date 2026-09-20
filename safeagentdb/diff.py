"""
diff.py - Row-level diff engine between sandbox state and original snapshot.

Computes INSERT / UPDATE / DELETE changesets per table and renders
a Rich-powered visual diff dashboard with per-row Pydantic validation.

Non-TTY safe: display() auto-detects whether stdout supports ANSI colors.
When piped to a file or CI log, it falls back to clean plain-text output.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum
from io import StringIO
from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from safeagentdb.models import get_validator, missing_validator_message


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


@dataclass
class RowDiff:
    """A single row-level change."""

    table: str
    diff_type: DiffType
    pk: dict[str, Any]
    old: dict[str, Any] | None = None
    new: dict[str, Any] | None = None
    require_validator: bool = True

    def changed_columns(self) -> list[str]:
        if self.diff_type != DiffType.UPDATE or not self.old or not self.new:
            return []
        return [k for k in self.new if self.old.get(k) != self.new[k]]

    def validate(self) -> tuple[bool, str]:
        """Report whether this row would be accepted at commit time.

        A missing validator is treated exactly as sync.validate_row() treats it,
        so the diff dashboard can never show a green light for a changeset the
        commit will refuse.
        """
        if self.diff_type == DiffType.DELETE:
            return True, "OK (delete)"

        row_data = self.new
        if row_data is None:
            return False, "Missing row data"

        validator_cls = get_validator(self.table)
        if validator_cls is None:
            if self.require_validator:
                return False, missing_validator_message(self.table)
            return True, "WARNING: no validator, row unchecked"

        try:
            validator_cls.model_validate(row_data)
            return True, "OK"
        except Exception as e:
            first_error = str(e).split("\n")[1] if "\n" in str(e) else str(e)
            return False, first_error.strip()


@dataclass
class ChangeSet:
    """Full set of diffs across all tables."""

    diffs: list[RowDiff] = field(default_factory=list)
    unsupported_constraints: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return len(self.diffs) == 0

    @property
    def summary(self) -> dict[str, int]:
        counts = {"INSERT": 0, "UPDATE": 0, "DELETE": 0}
        for d in self.diffs:
            counts[d.diff_type.value] += 1
        return counts

    def validate_all(self) -> list[tuple[RowDiff, bool, str]]:
        return [(d, *d.validate()) for d in self.diffs]

    @property
    def is_valid(self) -> bool:
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
                "[bold green][SAFE] AI CHANGES VERIFIED -- SAFE TO COMMIT[/bold green]",
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
            pk_str = _format_pk(d.pk)
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
                    pk_str = _format_pk(d.pk)
                    console.print(
                        f"  [bold red]>[/bold red] [cyan]{d.table}[/cyan] "
                        f"pk={pk_str}: [red]{msg}[/red]"
                    )
            console.print()

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
        if self.is_empty:
            return "\n".join(
                ["SafeAgentDB: No changes detected."] + self._plain_unsupported_lines()
            )

        all_valid = self.is_valid
        s = self.summary

        if all_valid:
            header = "[SAFE] AI CHANGES VERIFIED -- SAFE TO COMMIT"
        else:
            header = "[BLOCKED] SAFETY ALERT -- INVALID DATA DETECTED"

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
            pk_str = _format_pk(d.pk)
            op = d.diff_type.value

            if d.diff_type == DiffType.INSERT and d.new:
                for col, val in d.new.items():
                    display_rows.append((d.table, op, pk_str, col, "--", str(val), status))
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

    def _plain_unsupported_lines(self) -> list[str]:
        if not self.unsupported_constraints:
            return []
        lines = ["[NOT ENFORCED IN SANDBOX] violations surface only in production:"]
        lines.extend(f"  - {item}" for item in self.unsupported_constraints)
        lines.append("")
        return lines


def _build_column_rows(d: RowDiff) -> list[tuple[str, Text, Text]]:
    rows: list[tuple[str, Text, Text]] = []

    if d.diff_type == DiffType.INSERT and d.new:
        for col, val in d.new.items():
            rows.append((
                col,
                Text("--", style="dim"),
                Text(str(val), style="green"),
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
) -> ChangeSet:
    changeset = ChangeSet(unsupported_constraints=list(unsupported_constraints or []))

    for table_name in sorted(set(original_snapshot) | set(current_snapshot)):
        pks = pk_columns.get(table_name, ["id"])
        orig_rows = {_pk_key(r, pks): r for r in original_snapshot.get(table_name, [])}
        curr_rows = {_pk_key(r, pks): r for r in current_snapshot.get(table_name, [])}

        for pk_key in sorted(orig_rows.keys() - curr_rows.keys()):
            changeset.diffs.append(
                RowDiff(
                    table=table_name,
                    diff_type=DiffType.DELETE,
                    require_validator=require_validators,
                    pk=dict(zip(pks, pk_key)),
                    old=orig_rows[pk_key],
                )
            )

        for pk_key in sorted(curr_rows.keys() - orig_rows.keys()):
            changeset.diffs.append(
                RowDiff(
                    table=table_name,
                    diff_type=DiffType.INSERT,
                    require_validator=require_validators,
                    pk=dict(zip(pks, pk_key)),
                    new=curr_rows[pk_key],
                )
            )

        for pk_key in sorted(orig_rows.keys() & curr_rows.keys()):
            if orig_rows[pk_key] != curr_rows[pk_key]:
                changeset.diffs.append(
                    RowDiff(
                        table=table_name,
                        diff_type=DiffType.UPDATE,
                    require_validator=require_validators,
                        pk=dict(zip(pks, pk_key)),
                        old=orig_rows[pk_key],
                        new=curr_rows[pk_key],
                    )
                )

    return changeset


def _pk_key(row: dict[str, Any], pk_cols: list[str]) -> tuple:
    return tuple(row[c] for c in pk_cols)


def _format_pk(pk: dict[str, Any]) -> str:
    if len(pk) == 1:
        return str(next(iter(pk.values())))
    return ",".join(f"{k}={v}" for k, v in pk.items())
