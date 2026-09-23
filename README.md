<p align="center">
  <h1 align="center">SafeAgentDB</h1>
  <p align="center"><strong>The Shadow-Sandbox DB Layer for AI Agents</strong></p>
  <p align="center">Let AI modify your production database. Without the terror.</p>
</p>

<p align="center">
  <a href="https://pypi.org/project/safeagentdb/"><img src="https://img.shields.io/pypi/v/safeagentdb?color=blue&label=PyPI" alt="PyPI"></a>
  <a href="https://pypi.org/project/safeagentdb/"><img src="https://img.shields.io/pypi/pyversions/safeagentdb" alt="Python"></a>
  <a href="https://github.com/arhandonerkaya/safeagentdb/blob/main/LICENSE"><img src="https://img.shields.io/github/license/arhandonerkaya/safeagentdb?color=green" alt="License"></a>
  <a href="https://pypi.org/project/safeagentdb/"><img src="https://img.shields.io/pypi/dm/safeagentdb?color=orange" alt="Downloads"></a>
  <a href="https://github.com/arhandonerkaya/safeagentdb/actions/workflows/ci.yml"><img src="https://github.com/arhandonerkaya/safeagentdb/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/arhandonerkaya/safeagentdb/tree/main/tests"><img src="https://img.shields.io/badge/tests-139%20passing-brightgreen" alt="Tests"></a>
</p>

---

<p align="center">
  <img src="https://raw.githubusercontent.com/arhandonerkaya/safeagentdb/main/docs/assets/scenario-1-pass.png" width="800" alt="SafeAgentDB - Safe AI Changes Verified">
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

## The 6 Safety Gates

Every call to `commit_to_production()` passes through 6 sequential gates. If **any** gate fails, the **entire** transaction rolls back. Nothing touches production.

```
Gate 1: TENANT ISOLATION AT CLONE
  Only rows matching your tenant_id are copied into the sandbox.
  Other tenants' data never enters memory. Ever.

Gate 2: ROW-LEVEL DIFFING
  Every change is computed as an explicit INSERT / UPDATE / DELETE
  with before/after values, keyed on the primary key or your row_key.
  A table whose rows cannot be identified is refused outright.

Gate 3: PYDANTIC RE-VALIDATION
  Every row is validated against your SafeModel schema.
  Strict mode. No type coercion. Bad data = instant rejection.

Gate 4: ROW KEY + TENANT WHERE CLAUSE
  Every UPDATE/DELETE statement carries WHERE <row key> AND tenant_id = ?
  A statement that would match rows by tenant alone is refused, not run.

Gate 5: OPTIMISTIC CONCURRENCY CHECK
  Each target row is re-read and compared against the values captured at
  clone time. If another process changed it, you get a ConflictError
  naming the drifted columns -- not a silent overwrite.

Gate 6: ATOMIC SYNC
  The entire changeset executes in ONE transaction, writing only the
  columns the agent actually changed.
```

See [Guarantees and limits](#guarantees-and-limits) for what these gates
deliberately do **not** cover.

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

```
+------------------------------------ BLOCKED ------------------------------------+
|  [BLOCKED] SAFETY ALERT -- INVALID DATA DETECTED                                |
+---------------------------------------------------------------------------------+
  ~1 update

  > tasks pk=1: Input should be 'todo', 'in_progress' or 'done'
```

---

## API Reference

### `ShadowDB`

The core context manager. Creates an isolated sandbox from your production database.

```python
ShadowDB(
    prod_engine: Engine,            # Any SQLAlchemy engine (Postgres, MySQL, SQLite, ...)
    tables: Sequence[str],          # Table names to clone into the sandbox
    tenant_id: Any,                 # The tenant/user ID to scope all operations to
    tenant_column: str = "user_id", # Column name used for tenant filtering
    *,
    row_key: dict[str, list[str]] | None = None,  # Explicit row key per table
    on_conflict: "abort" | "ignore" = "abort",    # What to do on production drift
    require_validators: bool = True,              # Missing SafeModel: error or warning
)
```

**Keyword options:**

| Option | Default | What it does |
|--------|---------|--------------|
| `row_key` | `None` | Explicit row-identifying columns per table, e.g. `{"events": ["tenant_id", "event_uuid"]}`. Required for a table with no primary key, and usable to override one. The columns must exist and must be unique across the cloned rows, or `SchemaError` is raised. |
| `on_conflict` | `"abort"` | `"abort"` raises `ConflictError` when a production row changed between clone and commit, rolling the whole changeset back. `"ignore"` skips the drifted row and applies the rest. |
| `require_validators` | `True` | `True`: a table with no registered `SafeModel` is a failed row in `diff()` and a `MissingValidatorError` at commit. `False`: a warning in both places, and the row is written unvalidated. |

> **Tables must have an identifiable row.** A table with no primary key and no
> `row_key` raises `SchemaError` at `__enter__`. Without a key, SafeAgentDB
> cannot tell two rows apart, and any `UPDATE` it built would match every row
> the tenant owns.

**Context Manager Lifecycle:**

| Phase | What Happens |
|-------|-------------|
| `__enter__` | 1. Reflects schema from production. 2. Creates an in-memory SQLite sandbox with foreign keys enforced, carrying constraints, unique indexes and column defaults across. 3. Resolves a row key per table, raising `SchemaError` if one is missing. 4. Clones only rows where `tenant_column = tenant_id`. 5. Snapshots the cloned state for later diffing. 6. Opens a SQLAlchemy `Session`. |
| *inside `with`* | AI operates freely on the sandbox via `execute()`, `query()`, or `session`. |
| `__exit__` | Session closed. Sandbox engine disposed. All in-memory data destroyed. |

**Methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `execute` | `(sql: str, params: dict \| None) -> CursorResult` | Execute raw SQL inside the sandbox. Wraps the string in `text()` automatically so AI agents do not need to import it. Returns a standard SQLAlchemy `CursorResult`. |
| `query` | `(sql: str, params: dict \| None) -> list[dict]` | Execute a SELECT and return results as a list of plain dictionaries. Convenience method for AI agents that work with JSON-like data. |
| `diff` | `() -> ChangeSet` | Flushes pending changes, snapshots the current sandbox state, and computes a row-level diff against the original clone. Returns a `ChangeSet` object. |
| `commit_to_production` | `() -> int` | Runs all 6 safety gates and syncs approved changes to production in one atomic transaction, writing only the columns the agent changed. Returns the number of rows **actually written**, summed from each statement's rowcount. Raises `ConflictError` on production drift, `SyncError` on tenant breach or a missing row key, `MissingValidatorError` when a table has no `SafeModel`, `pydantic.ValidationError` on schema violations. Can only be called once per sandbox (double-commit raises `SyncError`). |

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `clone_stats` | `dict[str, int]` | Number of rows cloned per table when the sandbox was created. |
| `tables` | `list[str]` | Table names available in this sandbox. |
| `dialect` | `str` | Production database dialect name (`'postgresql'`, `'mysql'`, `'sqlite'`). |
| `row_keys` | `dict[str, list[str]]` | The row-identifying columns in use for each cloned table. |
| `unsupported_constraints` | `list[str]` | Schema elements that could not be reproduced in the sandbox, and are therefore **not enforced** there. See below. |
| `session` | `Session` | Raw SQLAlchemy `Session` for ORM-style operations if needed. |

**`unsupported_constraints`**

The sandbox is SQLite; production usually is not. Anything that could not be
carried across is listed here rather than dropped silently, and is rendered in
both the Rich and plain diff output under a `NOT ENFORCED IN SANDBOX` heading:

```python
with ShadowDB(prod_engine, tables=["users"], tenant_id=42) as sandbox:
    for item in sandbox.unsupported_constraints:
        print(item)
    # users.prefs: JSONB stored as TEXT in the sandbox; values valid here may
    #   still be rejected by production
    # users.code: server default "gen_random_uuid()" has no SQLite equivalent
```

A violation of anything on this list will not be caught by `diff()`. It surfaces
only when production rejects the commit.

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
| `validate_row(table_name, row_data, *, require_validator=True)` | Validates a dict against the registered model. With `require_validator=True` (the default) a missing validator raises `MissingValidatorError`; with `False` it warns and returns `None`. Raises `ValidationError` on bad data. |
| `missing_validator_message(table_name)` | The single wording used by both `RowDiff.validate()` and `validate_row()`, so the dashboard and the commit can never disagree. |

### `ChangeSet`

Returned by `sandbox.diff()`. Contains the full set of row-level changes.

| Member | Type | Description |
|--------|------|-------------|
| `diffs` | `list[RowDiff]` | Raw list of individual row changes. |
| `is_empty` | `bool` | `True` if the AI made no changes. |
| `is_valid` | `bool` | `True` if every row passes Pydantic validation. With `require_validators=True`, a table with no registered `SafeModel` makes this `False`. Check it before calling `commit_to_production()`. |
| `unsupported_constraints` | `list[str]` | Schema elements not enforced in the sandbox, carried through from `ShadowDB` so the rendered diff can warn about them. |
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
| `pk` | `dict[str, Any]` | Row key values identifying the row: the primary key, or the `row_key` you supplied. |
| `old` | `dict \| None` | Row data before the change (`None` for INSERTs). |
| `new` | `dict \| None` | Row data after the change (`None` for DELETEs). |
| `changed_columns()` | `-> list[str]` | Column names that differ between `old` and `new` (UPDATEs only). |
| `require_validator` | `bool` | Whether a missing `SafeModel` makes this row invalid. Set from `ShadowDB(require_validators=...)`. |
| `validate()` | `-> tuple[bool, str]` | Runs Pydantic validation on this row. Returns `(is_valid, message)`. Treats a missing validator exactly as the commit will. |

### Errors

All exceptions live in `safeagentdb.errors` and are importable from
`safeagentdb`. Every one derives from `SafeAgentDBError`, so a whole agent
session can be wrapped in a single `except`.

```
SafeAgentDBError
|-- SchemaError            production schema cannot be sandboxed safely
|-- SyncError              a changeset was rejected or could not be applied
|   +-- ConflictError      production drifted between clone and commit
+-- MissingValidatorError  no SafeModel registered and one is required
```

| Exception | Raised when |
|-----------|-------------|
| `SchemaError` | A cloned table has no primary key and no usable `row_key`; a `row_key` names missing columns or is not unique in the cloned rows; the production schema cannot be reproduced in SQLite. |
| `SyncError` | Tenant isolation is breached, an `UPDATE`/`DELETE` has no row-identifying key, a row key disagrees with the declared one, a table is missing from production metadata, or the sandbox is committed twice. |
| `ConflictError` | A production row changed or was deleted between clone and commit, or a row key matches more than one row. Carries `.table`, `.row_key` and `.columns` (the drifted column names). A subclass of `SyncError`. |
| `MissingValidatorError` | A table has no registered `SafeModel` and `require_validators` is `True`. Also subclasses `KeyError`, so 0.1.x handlers keep working. |
| `MissingValidatorWarning` | Not an error: warned when `require_validators=False` and a row is written unvalidated. |

**Handling a conflict:**

```python
from safeagentdb import ConflictError

try:
    sandbox.commit_to_production()
except ConflictError as exc:
    print(f"{exc.table} row {exc.row_key} drifted on {exc.columns}")
    # Nothing was written. Re-clone and let the agent try again,
    # or use ShadowDB(..., on_conflict="ignore") to skip drifted rows.
```

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

<p align="center">
  <img src="https://raw.githubusercontent.com/arhandonerkaya/safeagentdb/main/docs/assets/scenario-2-blocked.png" width="800" alt="SafeAgentDB - Blocked: Invalid Data Detected">
</p>

# Scenario 3: Even if AI could somehow craft a rogue row,
# every UPDATE/DELETE uses: WHERE pk = ? AND user_id = 42
# at the SQL level -- the database itself enforces the scope.
```

When an AI agent tries to access another tenant's data, the sandbox simply contains no rows for them -- the diff shows nothing changed:

<p align="center">
  <img src="https://raw.githubusercontent.com/arhandonerkaya/safeagentdb/main/docs/assets/scenario-3-approved.png" width="800" alt="SafeAgentDB - Tenant Isolation: No Changes Detected">
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

## Guarantees and limits

SafeAgentDB is a safety net, not a proof of correctness. This section is the
honest version of what that means.

### What the sandbox does catch

- **Schema violations** on every INSERT/UPDATE row, via your `SafeModel`
  (strict mode, no coercion, no extra columns).
- **`NOT NULL`, `PRIMARY KEY`, `UNIQUE`, `CHECK` and `FOREIGN KEY` violations**,
  for every constraint that could be reproduced in SQLite. Foreign keys are
  enforced (`PRAGMA foreign_keys=ON`).
- **Column defaults**, so an `INSERT` that omits a defaulted `NOT NULL` column
  behaves in the sandbox as it does in production.
- **Cross-tenant writes**, at clone time, at validation time, and in the `WHERE`
  clause of every statement.
- **Unidentifiable rows**: a table with no primary key and no `row_key` is
  refused rather than guessed at.
- **Production drift**: a row changed by another process between clone and
  commit is a `ConflictError`, not a silent overwrite.

### What the sandbox cannot catch

- **Anything listed in `unsupported_constraints`.** Dialect-specific column
  types are stored as `TEXT`/`VARCHAR`, so a malformed UUID or a non-JSON string
  passes in the sandbox and is rejected by production. Server defaults with no
  SQLite equivalent, foreign keys pointing outside the cloned tables, and
  `CHECK` constraints that reflection could not reproduce are all listed there.
- **Uniqueness against rows you did not clone.** The sandbox holds one tenant's
  rows. A value that is unique within that tenant can still collide with another
  tenant's row, and that only surfaces when production rejects the commit.
- **Database-side logic.** Triggers, rules, row-level security policies, stored
  procedures, generated columns, partial and functional indexes, deferrable
  constraints and exclusion constraints are not cloned and never run in the
  sandbox.
- **Dialect semantics.** SQLite has dynamic typing, different collation and
  case-sensitivity rules, and no fixed-width integer overflow. A value SQLite
  accepts may be rejected or stored differently by PostgreSQL or MySQL.
- **Concurrency beyond the rows in the changeset.** The optimistic check covers
  the rows being written. A row the agent merely read is not checked, so a
  decision based on stale data can still be applied. How much isolation the
  check itself has is decided by your database's transaction isolation level.
- **Value comparison across driver round-trips.** Drift detection compares
  Python values with `!=`. A type that does not round-trip identically through
  your driver can register as a spurious conflict.
- **Scale.** The entire tenant scope is copied into memory. This is designed for
  one tenant's working set, not for a full-table migration.

### Operational notes

- The sandbox is **always in-memory SQLite**, whatever your production dialect.
- `MetaData.reflect` resolves foreign keys, so asking for one table may clone the
  tables it references as well. Check `clone_stats` to see what was copied.
- Foreign key enforcement is suspended while the tenant's rows are loaded, since
  a tenant-scoped clone is a partial view and may reference parents that were
  not copied. It is on for everything the agent does afterwards.
- `commit_to_production()` can be called once per sandbox.

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
  |-- 3. Compute row-level diff vs original clone, keyed on the row key
  |
  |-- FOR EACH diff:
  |     |-- 4. Assert tenant_id matches in row data      --> SyncError
  |     |-- 5. Run Pydantic model_validate(row)          --> ValidationError
  |     |                                                    MissingValidatorError
  |     |-- 6. INSERT? insert(table).values(row); count rowcount
  |     |
  |     |-- 7. Assert the row key is present and correct --> SyncError
  |     |      (an UPDATE/DELETE with no key is refused, never executed)
  |     |
  |     |-- 8. SELECT the live row by row key + tenant   --> ConflictError
  |     |      and compare every column against the clone-time values
  |     |
  |     '-- 9. Build the SQLAlchemy Core statement:
  |           |-- UPDATE: update(table)
  |           |             .where(row key AND tenant_id)
  |           |             .values(only the changed columns)
  |           '-- DELETE: delete(table).where(row key AND tenant_id)
  |           '-- rowcount == 0 --> ConflictError (the row vanished)
  |
  '-- 10. All statements execute inside engine.begin()   --> Single transaction
          '-- Any failure --> full rollback, zero writes
```

No raw SQL strings are generated. Every statement uses SQLAlchemy Core constructs (`insert()`, `update()`, `delete()`), making the sync engine dialect-agnostic and SQL-injection-proof.

---

## Architecture

```
safeagentdb/
|-- __init__.py     Public API: ShadowDB, SafeModel, ChangeSet, RowDiff, DiffType, errors
|-- errors.py       SafeAgentDBError hierarchy: SchemaError, SyncError, ConflictError, ...
|-- engine.py       Schema reflection and translation, sandbox creation, row cloning
|-- sandbox.py      ShadowDB context manager with execute(), query(), diff(), commit_to_production()
|-- models.py       SafeModel base class + auto-registration validator registry
|-- diff.py         Row-level diff engine + Rich dashboard renderer + plain-text fallback
|-- sync.py         Atomic production sync: tenant guards, row-key guard, concurrency check
'-- py.typed        PEP 561 type checker marker

tests/
|-- test_core.py        End-to-end release check (script style)
|-- test_mega.py        Exhaustive coverage of the public API
|-- test_weaknesses.py  Regression guards for the four findings in docs/AUDIT.md
'-- test_hardening.py   The machinery added in 0.2.0

docs/
'-- AUDIT.md        The weakness audit those regression guards came from
```

Run the suite with `python -m pytest -q` and the linter with `ruff check .`;
both run in CI on Python 3.10, 3.11 and 3.12.

## License

MIT -- see [LICENSE](LICENSE).
