"""
Demonstrates Pydantic validation catching bad AI output BEFORE it touches production.
Shows the Rich visual diff with FAIL status on invalid rows.
"""

from typing import Literal

from pydantic import ValidationError
from rich.console import Console
from sqlalchemy import create_engine, text

from safeagentdb import ShadowDB, SafeModel

console = Console()


class TaskValidator(SafeModel):
    __table_name__ = "tasks"

    id: int
    user_id: int
    title: str
    status: Literal["todo", "in_progress", "done"]


# Setup production DB
prod_engine = create_engine("sqlite:///demo_validation.db", echo=False)
with prod_engine.begin() as conn:
    conn.execute(text("DROP TABLE IF EXISTS tasks"))
    conn.execute(text("""
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'todo'
        )
    """))
    conn.execute(text(
        "INSERT INTO tasks VALUES (1, 42, 'Ship feature', 'todo')"
    ))

# AI writes an invalid status
with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sandbox:
    sandbox.execute("UPDATE tasks SET status = 'yolo_swag' WHERE id = 1")

    changeset = sandbox.diff()
    changeset.print()

    try:
        sandbox.commit_to_production()
    except ValidationError as e:
        console.print(f"\n  [bold red]Sync BLOCKED by Pydantic validation:[/bold red]")
        console.print(f"  [red]{e}[/red]")
        console.print(f"\n  [green]Production DB is untouched. Crisis averted.[/green]\n")
