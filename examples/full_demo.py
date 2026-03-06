"""
SafeAgentDB - Full Demo: INSERT + UPDATE + DELETE in one changeset.
Shows the complete Rich dashboard with all operation types.
"""

from typing import Literal

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


# Setup production DB with sample data
prod_engine = create_engine("sqlite:///demo_full.db", echo=False)
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
    conn.execute(text("""
        INSERT INTO tasks (id, user_id, title, status) VALUES
            (1, 42, 'Deploy v2 API',       'todo'),
            (2, 42, 'Write unit tests',    'in_progress'),
            (3, 42, 'Fix login bug',       'todo'),
            (4, 99, 'Other user task',     'todo')
    """))

console.rule("[bold]SafeAgentDB - Full CRUD Demo[/bold]")

with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sandbox:

    console.print(f"\n  Sandbox loaded: [cyan]{sandbox.clone_stats}[/cyan]\n")

    # AI performs mixed operations
    sandbox.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
    sandbox.execute("UPDATE tasks SET title = 'Add integration tests' WHERE id = 2")
    sandbox.execute("DELETE FROM tasks WHERE id = 3")
    sandbox.execute(
        "INSERT INTO tasks (id, user_id, title, status) VALUES "
        "(5, 42, 'Review PR #101', 'todo')"
    )

    # Show the full dashboard
    changeset = sandbox.diff()
    changeset.print()

    # Programmatic checks
    console.print(f"  Valid: [{'green' if changeset.is_valid else 'red'}]"
                  f"{changeset.is_valid}[/]")
    console.print(f"  Summary: {changeset.summary}\n")

    # Sync
    affected = sandbox.commit_to_production()
    console.print(f"  [bold green]Synced {affected} row(s) to production.[/bold green]\n")

console.rule("[bold]Production State[/bold]")
with prod_engine.connect() as conn:
    for row in conn.execute(text("SELECT * FROM tasks ORDER BY id")).fetchall():
        console.print(f"  {row}")
console.print()
