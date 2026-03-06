<p align="center">
  <h1 align="center">SafeAgentDB</h1>
  <p align="center"><strong>The Shadow-Sandbox DB Layer for AI Agents</strong></p>
  <p align="center">Let AI modify your production database. Without the terror.</p>
</p>

<p align="center">
  <a href="https://pypi.org/project/safeagentdb/"><img src="https://img.shields.io/pypi/v/safeagentdb?color=blue&label=PyPI" alt="PyPI"></a>
  <a href="https://pypi.org/project/safeagentdb/"><img src="https://img.shields.io/pypi/pyversions/safeagentdb" alt="Python"></a>
  <a href="https://github.com/sippinonstraightchlorine/safeagentdb/blob/main/LICENSE"><img src="https://img.shields.io/github/license/sippinonstraightchlorine/safeagentdb?color=green" alt="License"></a>
  <a href="https://pypi.org/project/safeagentdb/"><img src="https://img.shields.io/pypi/dm/safeagentdb?color=orange" alt="Downloads"></a>
</p>

---

<p align="center">
  <img src="https://raw.githubusercontent.com/sippinonstraightchlorine/safeagentdb/main/docs/assets/scenario-1-pass.png" width="800" alt="SafeAgentDB - Safe AI Changes Verified">
</p>

---

```
Production DB                     In-Memory SQLite Sandbox
      |                                     |
      |--- clone tenant rows -------------->|
      |                                     |--- AI operates freely (CRUD)
      |                                     |--- Pydantic validates every row
      |                                     |--- Rich diff shows what changed
      |<-- atomic sync (on approval) -------|
      |                                     |--- sandbox destroyed
```

## The Problem

You are building AI-powered features. An agent that manages tasks. A copilot that updates billing. An assistant that edits user profiles. Your AI needs **write access** to the database.

Two things keep you up at night:

| Nightmare | What Happens |
|-----------|-------------|
| **AI Logical Error** | The LLM writes `status = 'yolo_swag'` instead of `'done'`. Or sets `balance = -99999`. Without a safety net, it goes straight to production. |
| **Multi-Tenancy Breach** | Your agent operates for User 42 but accidentally touches User 99's rows. One wrong `WHERE` clause = data breach. |

**SafeAgentDB eliminates both.** Every AI write is sandboxed, validated, diffed, tenant-scoped, and synced atomically -- or not at all.

## The 4 Safety Gates

Every call to `commit_to_production()` passes through 4 sequential gates. If **any** gate fails, the **entire** transaction rolls back. Nothing touches production.

```
Gate 1: TENANT ISOLATION AT CLONE
  Only rows matching your tenant_id are copied into the sandbox.
  Other tenants' data never enters memory. Ever.

Gate 2: ROW-LEVEL DIFFING
  Every change is computed as an explicit INSERT / UPDATE / DELETE
  with before/after values. You see exactly what the AI did.

Gate 3: PYDANTIC RE-VALIDATION
  Every row is validated against your SafeModel schema.
  Strict mode. No type coercion. Bad data = instant rejection.

Gate 4: ATOMIC SYNC + TENANT WHERE CLAUSE
  The entire changeset executes in ONE transaction.
  Every UPDATE/DELETE SQL statement includes WHERE tenant_id = ?
  as a hard constraint -- even if the AI tried to tamper with it.
```

## Getting Started

### 1. Install

```bash
pip install safeagentdb
```

With database drivers:

```bash
pip install safeagentdb[pg]      # PostgreSQL
pip install safeagentdb[mysql]   # MySQL / MariaDB
```

### 2. Define Your Schema Validator

Create a `SafeModel` subclass for each table an AI agent can write to. This is your **contract** -- any row that violates it will be rejected before it touches production.

```python
from typing import Literal
from safeagentdb import SafeModel

class TaskValidator(SafeModel):
    __table_name__ = "tasks"      # links this validator to the "tasks" table

    id: int
    user_id: int
    title: str
    status: Literal["todo", "in_progress", "done"]  # AI can ONLY write these values
```

`SafeModel` inherits from Pydantic `BaseModel` with `strict=True` and `extra="forbid"`. No silent type coercion. No extra fields sneaking through.

### 3. Sandbox the AI Agent

```python
from sqlalchemy import create_engine
from safeagentdb import ShadowDB

engine = create_engine("postgresql://user:pass@localhost/mydb")

with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
    # The AI does whatever it wants -- all writes stay in the sandbox
    sandbox.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
    sandbox.execute(
        "INSERT INTO tasks (id, user_id, title, status) "
        "VALUES (100, 42, 'AI-generated task', 'todo')"
    )

    # Review: see exactly what changed, with validation status
    sandbox.diff().print()

    # Approve: sync to production in a single atomic transaction
    sandbox.commit_to_production()
```

### 4. Review the Diff

`sandbox.diff().print()` renders a color-coded Rich dashboard:

```
+------------------------------------- SAFE --------------------------------------+
|  [SAFE] AI CHANGES VERIFIED -- SAFE TO COMMIT                                   |
+---------------------------------------------------------------------------------+
  +1 insert  ~1 update

                                 Row-Level Changes
+---------------------------------------------------------------------------------+
|     | Table | Op     | PK  | Column  | Old Value | New Value         | Valid.   |
|-----+-------+--------+-----+---------+-----------+-------------------+----------|
|  +  | tasks | INSERT | 100 | id      | --        | 100               | [PASS]   |
|     |       |        |     | user_id | --        | 42                |          |
|     |       |        |     | title   | --        | AI-generated task |          |
|     |       |        |     | status  | --        | todo              |          |
|     |       |        |     |         |           |                   |          |
|-----+-------+--------+-----+---------+-----------+-------------------+----------|
|  ~  | tasks | UPDATE | 1   | status  | todo      | done              | [PASS]   |
|     |       |        |     |         |           |                   |          |
+---------------------------------------------------------------------------------+
  >> ALL VALIDATIONS PASSED
```

**Color coding:** INSERT = green, UPDATE = yellow, DELETE = red. Validation badges: `[PASS]` green, `[FAIL]` red.

**Non-TTY safe:** When piped to a file or running in CI, `display()` automatically falls back to clean plain text with zero ANSI escape codes.

### 5. Handle Validation Failures

When the AI writes bad data, SafeAgentDB blocks the sync before anything touches production:

```python
with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
    sandbox.execute("UPDATE tasks SET status = 'yolo_swag' WHERE id = 1")

    changeset = sandbox.diff()
    changeset.print()     # Shows [BLOCKED] banner with [FAIL] badge

    if not changeset.is_valid:
        print("AI output rejected. Production untouched.")
    else:
        sandbox.commit_to_production()
```

The `[BLOCKED]` banner appears:

<p align="center">
  <img src="https://raw.githubusercontent.com/sippinonstraightchlorine/safeagentdb/main/docs/assets/scenario-2-blocked.png" width="800" alt="SafeAgentDB - Blocked: Invalid Data Detected">
</p>

---

## API Reference

### `ShadowDB`

The core context manager. Creates an isolated sandbox from your production database.

```python
ShadowDB(
    prod_engine: Engine,          # Any SQLAlchemy engine (Postgres, MySQL, SQLite, ...)
    tables: Sequence[str],        # Table names to clone into the sandbox
    tenant_id: Any,               # The tenant/user ID to scope all operations to
    tenant_column: str = "user_id"  # Column name used for tenant filtering
)
```

**Context Manager Lifecycle:**

| Phase | What Happens |
|-------|-------------|
| `__enter__` | 1. Reflects schema from production. 2. Creates in-memory SQLite sandbox. 3. Clones only rows where `tenant_column = tenant_id`. 4. Snapshots the cloned state for later diffing. 5. Opens a SQLAlchemy `Session`. |
| *inside `with`* | AI operates freely on the sandbox via `execute()`, `query()`, or `session`. |
| `__exit__` | Session closed. Sandbox engine disposed. All in-memory data destroyed. |

**Methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `execute` | `(sql: str, params: dict \| None) -> CursorResult` | Execute raw SQL inside the sandbox. Wraps the string in `text()` automatically so AI agents do not need to import it. Returns a standard SQLAlchemy `CursorResult`. |
| `query` | `(sql: str, params: dict \| None) -> list[dict]` | Execute a SELECT and return results as a list of plain dictionaries. Convenience method for AI agents that work with JSON-like data. |
| `diff` | `() -> ChangeSet` | Flushes pending changes, snapshots the current sandbox state, and computes a row-level diff against the original clone. Returns a `ChangeSet` object. |
| `commit_to_production` | `() -> int` | Runs all 4 safety gates and syncs approved changes to production in one atomic transaction. Returns the number of rows affected. Raises `SyncError` on tenant breach, `pydantic.ValidationError` on schema violations. Can only be called once per sandbox (double-commit raises `SyncError`). |

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `clone_stats` | `dict[str, int]` | Number of rows cloned per table when the sandbox was created. |
| `tables` | `list[str]` | Table names available in this sandbox. |
| `dialect` | `str` | Production database dialect name (`'postgresql'`, `'mysql'`, `'sqlite'`). |
| `session` | `Session` | Raw SQLAlchemy `Session` for ORM-style operations if needed. |

### `SafeModel`

Pydantic v2 base model for defining table schemas. Subclass it and set `__table_name__` to auto-register a validator.

```python
class SafeModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    __table_name__: ClassVar[str] = ""
```

| Feature | Behavior |
|---------|----------|
| `strict=True` | No silent type coercion. An `int` field rejects `"42"` (a string). |
| `extra="forbid"` | Any column not in the model raises a validation error. |
| `__table_name__` | Setting this on a subclass auto-registers it in the global validator registry. |
| Auto-registration | Happens at class definition time via `__init_subclass__`. No manual wiring needed. |

**Helper functions** (importable from `safeagentdb.models`):

| Function | Description |
|----------|-------------|
| `get_validator(table_name)` | Returns the `SafeModel` subclass registered for a table, or `None`. |
| `validate_row(table_name, row_data)` | Validates a dict against the registered model. Raises `KeyError` if no validator exists, `ValidationError` on bad data. |

### `ChangeSet`

Returned by `sandbox.diff()`. Contains the full set of row-level changes.

| Member | Type | Description |
|--------|------|-------------|
| `diffs` | `list[RowDiff]` | Raw list of individual row changes. |
| `is_empty` | `bool` | `True` if the AI made no changes. |
| `is_valid` | `bool` | `True` if every row passes Pydantic validation. Check this before calling `commit_to_production()`. |
| `summary` | `dict[str, int]` | `{"INSERT": n, "UPDATE": n, "DELETE": n}` |
| `print()` | `-> None` | Renders the Rich color-coded dashboard directly to the terminal. |
| `display()` | `-> str` | Returns the diff as a printable string. Auto-detects TTY: Rich ANSI in terminals, plain ASCII in pipes/CI. |
| `validate_all()` | `-> list[tuple[RowDiff, bool, str]]` | Returns per-row validation results: `(diff, is_valid, message)`. |

### `RowDiff`

A single row-level change.

| Field | Type | Description |
|-------|------|-------------|
| `table` | `str` | Table name. |
| `diff_type` | `DiffType` | `DiffType.INSERT`, `DiffType.UPDATE`, or `DiffType.DELETE`. |
| `pk` | `dict[str, Any]` | Primary key values identifying the row. |
| `old` | `dict \| None` | Row data before the change (`None` for INSERTs). |
| `new` | `dict \| None` | Row data after the change (`None` for DELETEs). |
| `changed_columns()` | `-> list[str]` | Column names that differ between `old` and `new` (UPDATEs only). |
| `validate()` | `-> tuple[bool, str]` | Runs Pydantic validation on this row. Returns `(is_valid, message)`. |

### `SyncError`

Exception raised when tenant isolation is breached or sync constraints are violated. Inherits from `Exception`.

### `DiffType`

Enum with three values: `INSERT`, `UPDATE`, `DELETE`.

---

## Advanced Scenarios

### Multi-Table Operations

SafeAgentDB supports sandboxing multiple tables at once. Validators are matched by `__table_name__`:

```python
class UserValidator(SafeModel):
    __table_name__ = "users"
    id: int
    user_id: int
    email: str
    plan: Literal["free", "pro", "enterprise"]

class InvoiceValidator(SafeModel):
    __table_name__ = "invoices"
    id: int
    user_id: int
    amount_cents: int
    status: Literal["pending", "paid", "refunded"]

with ShadowDB(engine, tables=["users", "invoices"], tenant_id=42) as sandbox:
    sandbox.execute("UPDATE users SET plan = 'pro' WHERE id = 1")
    sandbox.execute("UPDATE invoices SET status = 'paid' WHERE id = 1")
    sandbox.diff().print()
    sandbox.commit_to_production()
```

### Programmatic Approval Workflow

Use `is_valid` and `summary` to build approval logic without human intervention:

```python
with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
    run_ai_agent(sandbox)  # AI does its thing

    changeset = sandbox.diff()

    if changeset.is_empty:
        print("AI made no changes.")
    elif not changeset.is_valid:
        log.error("AI output rejected", extra=changeset.summary)
    elif changeset.summary["DELETE"] > 10:
        log.warning("AI wants to delete too many rows, needs human review")
    else:
        sandbox.commit_to_production()
```

### Tenant Security: What Gets Blocked

SafeAgentDB enforces tenant isolation at **three** levels:

```python
# Scenario 1: AI tries to INSERT a row for a different tenant
sandbox.execute("INSERT INTO tasks VALUES (99, 777, 'evil', 'todo')")
sandbox.commit_to_production()
# --> SyncError: "Tenant breach blocked on INSERT: row has user_id=777, expected 42."

# Scenario 2: AI tries to UPDATE a row to change its tenant
sandbox.execute("UPDATE tasks SET user_id = 777 WHERE id = 1")
sandbox.commit_to_production()
# --> SyncError: "Tenant breach blocked on UPDATE: row has user_id=777, expected 42."

# Scenario 3: Even if AI could somehow craft a rogue row,
# every UPDATE/DELETE uses: WHERE pk = ? AND user_id = 42
# at the SQL level -- the database itself enforces the scope.
```

When an AI agent tries to access another tenant's data, the sandbox simply contains no rows for them -- the diff shows nothing changed:

<p align="center">
  <img src="https://raw.githubusercontent.com/sippinonstraightchlorine/safeagentdb/main/docs/assets/scenario-3-approved.png" width="800" alt="SafeAgentDB - Tenant Isolation: No Changes Detected">
</p>

### Using the Raw SQLAlchemy Session

For ORM-style access, use `sandbox.session` directly:

```python
from sqlalchemy import text

with ShadowDB(engine, tables=["tasks"], tenant_id=42) as sandbox:
    result = sandbox.session.execute(text("SELECT count(*) FROM tasks"))
    count = result.scalar()
```

---

## Supported Databases

SafeAgentDB works with **any SQLAlchemy-supported database** as the production source. The sandbox is always in-memory SQLite.

| Database | Production | Sandbox | Notes |
|----------|-----------|---------|-------|
| **PostgreSQL** | Yes | Auto-mapped | `JSONB`, `UUID`, `ARRAY`, `INET`, `HSTORE`, `TSVECTOR` mapped to SQLite equivalents |
| **MySQL / MariaDB** | Yes | Auto-mapped | `ENUM`, `YEAR`, `TINYINT` mapped |
| **SQLite** | Yes | Direct clone | Schema cloned as-is |
| **SQL Server** | Yes | Auto-mapped | Via SQLAlchemy dialects |
| **Oracle** | Yes | Auto-mapped | Via SQLAlchemy dialects |

The production sync **always uses the original production metadata**. The type mapping only applies to the throwaway sandbox. Zero fidelity loss.

---

## Why Not Raw SQLAlchemy?

| Concern | Raw SQLAlchemy | SafeAgentDB |
|---------|---------------|-------------|
| **AI writes bad data** | Goes to production immediately | Pydantic validates every row first |
| **AI touches wrong tenant** | Your problem | Tenant guard on clone, on data, and in SQL WHERE |
| **Reviewing changes** | Write your own diff logic | `sandbox.diff().print()` with Rich dashboard |
| **Partial failures** | Manual transaction handling | Single atomic transaction, all-or-nothing |
| **Sandbox isolation** | Build it yourself | In-memory SQLite, auto-created, auto-destroyed |
| **Cross-dialect support** | Handle type mismatches yourself | Auto-maps Postgres/MySQL types to SQLite |

Raw SQLAlchemy is a general-purpose ORM. SafeAgentDB is a **purpose-built safety layer** for the specific threat model of AI agents writing to databases.

---

## How Sync Works Internally

```
sandbox.commit_to_production()
  |
  |-- 1. Flush sandbox session
  |-- 2. Snapshot current sandbox state
  |-- 3. Compute row-level diff vs original clone
  |
  |-- FOR EACH diff:
  |     |-- 4. Assert tenant_id matches in row data      --> SyncError
  |     |-- 5. Run Pydantic model_validate(row)          --> ValidationError
  |     '-- 6. Build SQLAlchemy Core statement:
  |           |-- INSERT: insert(table).values(row)
  |           |-- UPDATE: update(table).where(pk AND tenant_id).values(row)
  |           '-- DELETE: delete(table).where(pk AND tenant_id)
  |
  '-- 7. All statements execute inside engine.begin()    --> Single transaction
         '-- Any failure --> full rollback, zero writes
```

No raw SQL strings are generated. Every statement uses SQLAlchemy Core constructs (`insert()`, `update()`, `delete()`), making the sync engine dialect-agnostic and SQL-injection-proof.

---

## Architecture

```
safeagentdb/
|-- __init__.py     Public API: ShadowDB, SafeModel, ChangeSet, SyncError, RowDiff, DiffType
|-- engine.py       Schema reflection, dialect-aware type mapping, sandbox creation, row cloning
|-- sandbox.py      ShadowDB context manager with execute(), query(), diff(), commit_to_production()
|-- models.py       SafeModel base class + auto-registration validator registry
|-- diff.py         Row-level diff engine + Rich dashboard renderer + plain-text fallback
|-- sync.py         Atomic production sync with tenant guards and Pydantic re-validation
'-- py.typed        PEP 561 type checker marker
```

## License

MIT -- see [LICENSE](LICENSE).
