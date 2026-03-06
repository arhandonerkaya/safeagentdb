"""
release_test.py -- Production readiness check for SafeAgentDB v0.1.0

Simulates a real-world SaaS scenario end-to-end:
  1. Multi-table schema (users + tasks + invoices)
  2. Tenant-scoped sandbox creation
  3. AI-style mixed CRUD operations
  4. Rich diff dashboard with [SAFE] banner
  5. Pydantic validation catching bad AI output with [BLOCKED] banner
  6. Atomic sync to production
  7. Non-TTY plain-text fallback verification

Run:  python release_test.py
"""

import sys
from typing import Literal

from pydantic import ValidationError
from rich.console import Console
from sqlalchemy import create_engine, text

from safeagentdb import ShadowDB, SafeModel, ChangeSet, SyncError

console = Console()
PASS = 0
FAIL = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        console.print(f"  [bold green][PASS][/bold green] {label}")
    else:
        FAIL += 1
        msg = f"  [bold red][FAIL][/bold red] {label}"
        if detail:
            msg += f" -- {detail}"
        console.print(msg)


# ---- Schema setup (simulates a real SaaS with multiple tables) ----

prod_engine = create_engine("sqlite:///release_test.db", echo=False)

with prod_engine.begin() as conn:
    conn.execute(text("DROP TABLE IF EXISTS invoices"))
    conn.execute(text("DROP TABLE IF EXISTS tasks"))
    conn.execute(text("DROP TABLE IF EXISTS users"))

    conn.execute(text("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            email TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free'
        )
    """))
    conn.execute(text("""
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'todo',
            priority INTEGER NOT NULL DEFAULT 0
        )
    """))
    conn.execute(text("""
        CREATE TABLE invoices (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            amount_cents INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
        )
    """))

    # Tenant 42 data
    conn.execute(text("INSERT INTO users VALUES (1, 42, 'dev@saas.io', 'pro')"))
    conn.execute(text("INSERT INTO tasks VALUES (1, 42, 'Ship v2', 'todo', 1)"))
    conn.execute(text("INSERT INTO tasks VALUES (2, 42, 'Write docs', 'in_progress', 2)"))
    conn.execute(text("INSERT INTO tasks VALUES (3, 42, 'Fix bug #99', 'todo', 3)"))
    conn.execute(text("INSERT INTO invoices VALUES (1, 42, 9900, 'pending')"))

    # Tenant 99 data (must never leak into tenant 42 sandbox)
    conn.execute(text("INSERT INTO users VALUES (2, 99, 'hacker@evil.io', 'free')"))
    conn.execute(text("INSERT INTO tasks VALUES (4, 99, 'Steal data', 'todo', 0)"))
    conn.execute(text("INSERT INTO invoices VALUES (2, 99, 0, 'pending')"))


# ---- Validators ----

class UserValidator(SafeModel):
    __table_name__ = "users"
    id: int
    user_id: int
    email: str
    plan: Literal["free", "pro", "enterprise"]


class TaskValidator(SafeModel):
    __table_name__ = "tasks"
    id: int
    user_id: int
    title: str
    status: Literal["todo", "in_progress", "done"]
    priority: int


class InvoiceValidator(SafeModel):
    __table_name__ = "invoices"
    id: int
    user_id: int
    amount_cents: int
    status: Literal["pending", "paid", "refunded"]


# ===========================================================================
console.rule("[bold]SafeAgentDB v0.1.0 -- Release Test[/bold]")
console.print()

# ---- TEST 1: Tenant isolation ----
console.print("[bold cyan]Test 1: Tenant Isolation[/bold cyan]")

with ShadowDB(prod_engine, tables=["users", "tasks", "invoices"], tenant_id=42) as sb:
    users = sb.query("SELECT * FROM users")
    tasks = sb.query("SELECT * FROM tasks")
    invoices = sb.query("SELECT * FROM invoices")

    check("Only tenant 42 users cloned", len(users) == 1)
    check("Only tenant 42 tasks cloned", len(tasks) == 3)
    check("Only tenant 42 invoices cloned", len(invoices) == 1)
    check("Tenant 99 excluded from users", all(r["user_id"] == 42 for r in users))
    check("Tenant 99 excluded from tasks", all(r["user_id"] == 42 for r in tasks))
    check("Clone stats correct", sb.clone_stats == {"users": 1, "tasks": 3, "invoices": 1})
    check("Dialect detected", sb.dialect == "sqlite")

console.print()

# ---- TEST 2: Valid AI operations + Rich diff ----
console.print("[bold cyan]Test 2: Valid AI Operations + Rich Diff[/bold cyan]")

with ShadowDB(prod_engine, tables=["users", "tasks", "invoices"], tenant_id=42) as sb:
    # AI updates
    sb.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
    sb.execute("UPDATE tasks SET priority = 1 WHERE id = 2")
    sb.execute("DELETE FROM tasks WHERE id = 3")
    sb.execute(
        "INSERT INTO tasks (id, user_id, title, status, priority) "
        "VALUES (5, 42, 'Deploy to prod', 'todo', 1)"
    )
    sb.execute("UPDATE invoices SET status = 'paid' WHERE id = 1")

    changeset = sb.diff()

    check("Changeset not empty", not changeset.is_empty)
    check("Summary counts correct",
          changeset.summary == {"INSERT": 1, "UPDATE": 3, "DELETE": 1})
    check("All validations pass", changeset.is_valid)

    # Show the Rich dashboard
    console.print()
    changeset.print()

    # Sync to production
    affected = sb.commit_to_production()
    check("Sync returned correct count", affected == 5)

    # Double-commit guard
    try:
        sb.commit_to_production()
        check("Double-commit blocked", False, "Should have raised SyncError")
    except SyncError:
        check("Double-commit blocked", True)

console.print()

# ---- TEST 3: Pydantic validation catches bad AI output ----
console.print("[bold cyan]Test 3: Pydantic Validation Blocks Bad Data[/bold cyan]")

with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
    # AI writes an invalid status
    sb.execute("UPDATE tasks SET status = 'yolo_swag' WHERE id = 1")

    changeset = sb.diff()
    check("Changeset detects invalid data", not changeset.is_valid)

    # Show the BLOCKED dashboard
    console.print()
    changeset.print()

    try:
        sb.commit_to_production()
        check("Invalid sync blocked", False, "Should have raised ValidationError")
    except ValidationError:
        check("Invalid sync blocked by Pydantic", True)

console.print()

# ---- TEST 4: Non-TTY plain-text fallback ----
console.print("[bold cyan]Test 4: Non-TTY Plain-Text Fallback[/bold cyan]")

with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sb:
    sb.execute("UPDATE tasks SET status = 'done' WHERE id = 2")
    changeset = sb.diff()

    # Force non-TTY by rendering plain text directly
    plain = changeset._render_plain()
    check("Plain text contains [SAFE] header", "[SAFE]" in plain)
    check("Plain text contains table headers", "Table" in plain and "Column" in plain)
    check("Plain text has no ANSI escapes", "\x1b[" not in plain)
    check("Plain text contains PASS", "PASS" in plain)

console.print()

# ---- TEST 5: Verify production state after all tests ----
console.print("[bold cyan]Test 5: Production State Integrity[/bold cyan]")

with prod_engine.connect() as conn:
    tasks = conn.execute(text("SELECT * FROM tasks ORDER BY id")).fetchall()
    task_map = {r[0]: r for r in tasks}

    check("Task 1 status is 'done'", task_map[1][3] == "done")
    check("Task 2 priority updated", task_map[2][4] == 1)
    check("Task 3 deleted", 3 not in task_map)
    check("Task 5 inserted", 5 in task_map and task_map[5][2] == "Deploy to prod")
    check("Tenant 99 task untouched", 4 in task_map and task_map[4][1] == 99)

    invoices = conn.execute(text("SELECT * FROM invoices ORDER BY id")).fetchall()
    check("Invoice 1 marked paid", invoices[0][3] == "paid")
    check("Tenant 99 invoice untouched", invoices[1][1] == 99 and invoices[1][3] == "pending")

console.print()

# ---- TEST 6: Import surface ----
console.print("[bold cyan]Test 6: Public API Imports[/bold cyan]")

from safeagentdb import ShadowDB, SafeModel, ChangeSet, RowDiff, DiffType, SyncError
check("ShadowDB importable", ShadowDB is not None)
check("SafeModel importable", SafeModel is not None)
check("ChangeSet importable", ChangeSet is not None)
check("RowDiff importable", RowDiff is not None)
check("DiffType importable", DiffType is not None)
check("SyncError importable", SyncError is not None)

console.print()

# ---- Final verdict ----
console.rule("[bold]Results[/bold]")
total = PASS + FAIL
if FAIL == 0:
    console.print(
        f"\n  [bold green]{PASS}/{total} checks passed. "
        f"SafeAgentDB v0.1.0 is READY FOR RELEASE.[/bold green]\n"
    )
else:
    console.print(
        f"\n  [bold red]{FAIL}/{total} checks FAILED. "
        f"Fix issues before release.[/bold red]\n"
    )
    sys.exit(1)
