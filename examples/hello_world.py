"""
SafeAgentDB - Hello World Example

Demonstrates the full lifecycle:
1. Set up a "production" SQLite DB with tenant data
2. Clone tenant rows into a shadow sandbox
3. Simulate an AI making changes
4. Show the Rich visual diff with validation status
5. Sync approved changes back atomically
"""

from typing import Literal

from sqlalchemy import create_engine, text

from safeagentdb import ShadowDB, SafeModel


# -- Step 1: Define a Pydantic validator for the "tasks" table --

class TaskValidator(SafeModel):
    __table_name__ = "tasks"

    id: int
    user_id: int
    title: str
    status: Literal["todo", "in_progress", "done"]


# -- Step 2: Set up a fake "production" database --

prod_engine = create_engine("sqlite:///demo_prod.db", echo=False)

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
            (3, 99, 'Hack the planet',     'todo')
    """))


# -- Step 3: Use ShadowDB to sandbox AI operations for user_id=42 --

from rich.console import Console
console = Console()

console.rule("[bold]SafeAgentDB - Hello World[/bold]")

with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sandbox:

    # Verify: only user 42's rows are in the sandbox
    rows = sandbox.query("SELECT * FROM tasks")
    console.print(f"\nSandbox contains [cyan]{len(rows)}[/cyan] rows (tenant_id=42 only):")
    for r in rows:
        console.print(f"  [dim]{r}[/dim]")

    console.print(f"\n  Clone stats: [green]{sandbox.clone_stats}[/green]")
    console.print(f"  Production dialect: [yellow]{sandbox.dialect}[/yellow]\n")

    # Simulate AI operations
    sandbox.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
    sandbox.execute(
        "INSERT INTO tasks (id, user_id, title, status) VALUES (4, 42, 'Review PR #88', 'todo')"
    )

    # Review the diff BEFORE syncing (Rich dashboard)
    changeset = sandbox.diff()
    changeset.print()

    # Approve and sync to production
    affected = sandbox.commit_to_production()
    console.print(f"  Synced [bold green]{affected}[/bold green] row(s) to production.\n")

# -- Step 4: Verify production state --

console.rule("[bold]Production DB After Sync[/bold]")
with prod_engine.connect() as conn:
    for row in conn.execute(text("SELECT * FROM tasks ORDER BY id")).fetchall():
        console.print(f"  {row}")

console.print("\n[dim]Sandbox destroyed. No lingering state.[/dim]")
