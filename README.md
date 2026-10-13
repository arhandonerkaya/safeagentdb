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

## The 7 Safety Gates

Every call to `commit_to_production()` passes through 7 sequential gates. If **any** gate fails, the **entire** transaction rolls back. Nothing touches production.

```
Gate 1: TENANT ISOLATION AT CLONE
  Only rows matching your tenant_id are copied into the sandbox. Two
  deliberate exceptions: tables you list in reference_tables are cloned
  in full, and at commit the cascade check reads the tenant column of
  rows that reference a row being deleted, so it can name the tenants a
  referential action would reach.

Gate 2: ROW-LEVEL DIFFING
  Every change is computed as an explicit INSERT / UPDATE / DELETE
  with before/after values, keyed on the primary key or your row_key.
  A table whose rows cannot be identified is refused outright, and so is
  a row whose key the agent changed with UPDATE (see "Key changes").

Gate 3: PYDANTIC RE-VALIDATION
  Every INSERT and UPDATE row is validated against your SafeModel, as
  production will hold it (JSON decoded, timestamps parsed). Strict mode.
  No type coercion. The message names every failing field and why.

Gate 4: ROW KEY + TENANT WHERE CLAUSE
  Every UPDATE/DELETE statement carries WHERE <row key> AND tenant_id = ?
  A statement that would match rows by tenant alone is refused, not run.

Gate 5: NO UNSEEN WRITES
  A DELETE, or an UPDATE of a referenced column, that would make production
  run ON DELETE / ON UPDATE CASCADE, SET NULL or SET DEFAULT on rows the
  diff does not show is refused with CascadeError, unless you pass
  allow_cascades=True. Another tenant's rows are refused either way.

Gate 6: COMPARE AND SWAP
  The clone-time values are part of the WHERE clause, so checking and
  writing are a single statement with no window between them. If another
  process changed the row first, the statement matches nothing and you
  get a ConflictError naming the drifted columns -- not a silent overwrite.

Gate 7: ATOMIC SYNC
  The entire changeset executes in ONE transaction, in unit-of-work order
  (INSERTs parents first, then UPDATEs, then DELETEs children first),
  writing only the columns the agent actually changed.
```

The gates that need only the sandbox and the schema also run inside `diff()`,
through the same helpers the commit uses: validation, the tenant guard, row-key
problems, key changes, generated keys, values that cannot be decoded, and rows
that reference each other in a cycle. Three checks need production rows and are
commit-only: drift since the clone, uniqueness against rows outside the
tenant, and referential actions on rows the sandbox did not clone. A clean
banner says so.

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
    changeset = sandbox.diff()
    changeset.print()

    # Approve: commit exactly what was reviewed, and nothing else
    sandbox.commit_to_production(changeset=changeset)
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
        sandbox.commit_to_production(changeset=changeset)
```

The `[BLOCKED]` banner appears:

<p align="center">
  <img src="https://raw.githubusercontent.com/arhandonerkaya/safeagentdb/main/docs/assets/scenario-2-blocked.png" width="800" alt="SafeAgentDB - Blocked: Invalid Data Detected">
</p>

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
    row_key: dict[str, list[str]] | None = None,   # Explicit row key per table
    reference_tables: Sequence[str] | None = None, # Shared lookup tables, cloned in full
    on_conflict: "abort" | "ignore" = "abort",     # What to do on production drift
    require_validators: bool = True,               # Missing SafeModel: error or warning
    allow_cascades: bool = False,                  # Let referential actions run in-tenant
)
```

`tenant_id` is converted once, at `__enter__`, to the Python type of the tenant
column, so `"42"` taken from a URL works for an integer column and every later
comparison is like with like. A value that does not convert exactly
(`"forty-two"`, `"42.0"`) raises `UsageError`.

**Keyword options:**

| Option | Default | What it does |
|--------|---------|--------------|
| `row_key` | `None` | Explicit row-identifying columns per table, e.g. `{"events": ["tenant_id", "event_uuid"]}`. Required for a table with no primary key, and usable to override one. The columns must exist and must be unique across the cloned rows, or `SchemaError` is raised. |
| `reference_tables` | `None` | Tables to clone **in full**, ignoring the tenant filter. Use it for shared lookup tables (statuses, currencies, plans) so that foreign keys pointing at them are enforced in the sandbox. See [Reference tables](#reference-tables). |
| `on_conflict` | `"abort"` | `"abort"` raises `ConflictError` when a production row changed between clone and commit, rolling the whole changeset back. `"ignore"` **applies the changeset in part**: it skips the drifted row, applies the rest, records each skip in `skipped_conflicts` and raises a `ConflictWarning` per skipped row. |
| `require_validators` | `True` | `True`: a table with no registered `SafeModel` is a failed row in `diff()` and a `MissingValidatorError` at commit. `False`: a warning in both places, and the row is written unvalidated. |
| `allow_cascades` | `False` | `False`: a `DELETE`, or an `UPDATE` of a referenced column, that would make production run a referential action (`CASCADE`, `SET NULL`, `SET DEFAULT`) on any existing row raises `CascadeError`, because that effect is not in the diff that was reviewed. `True`: such actions may run on this tenant's rows. Rows of another tenant, or rows whose owner cannot be determined, are refused either way. A cascade the diff already shows -- the child table is cloned and the agent's delete removed the children in the sandbox too -- needs no opt-in, because the children are deleted first and the parent's `DELETE` then triggers nothing. |

#### Key changes

A changeset is keyed on the row key, so an `UPDATE` that changes a row's key
would reach production as a `DELETE` of the old key plus an `INSERT` of the new
one. Production would then run the `ON DELETE` action of every foreign key
pointing at the row, not the `ON UPDATE` action the agent's statement
triggered: children an `UPDATE` would re-point get deleted instead, or the
commit fails on a `RESTRICT`. So a key change is refused, in `diff()` (as a
blocking error) and at commit, with a message saying key changes must go
through the application.

Key changes are recorded exactly, not guessed from values: the sandbox installs
`TEMP` triggers that log every `UPDATE` changing a row key. A key changed and
changed back is not refused, nor is a `DELETE` plus an `INSERT` the agent issued
as such -- those are applied as exactly that. Swapping two rows' keys is a key
change and is refused. Dropping or altering the tracking objects is detected and
raises `SchemaError`.

> **Tables must have an identifiable row.** A table with no primary key and no
> `row_key` raises `SchemaError` at `__enter__`. Without a key, SafeAgentDB
> cannot tell two rows apart, and any `UPDATE` it built would match every row
> the tenant owns.

#### Generated keys

A `serial` or identity primary key is filled by a sequence that lives in
production. SQLite has no equivalent, so the sandbox fills the column with a
**placeholder** and the diff says so rather than showing an id that will not
survive:

```
 + | tasks | INSERT | pending | id      | -- | (assigned by production)
   |       |        |         | user_id | -- | 42
   |       |        |         | title   | -- | Ship v3
```

At commit the column is left out of the `INSERT`, production's sequence assigns
the real key, and it is read back and reported:

```python
with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sandbox:
    sandbox.execute("INSERT INTO tasks (user_id, title) VALUES (42, 'Ship v3')")
    sandbox.commit_to_production()

    for key in sandbox.assigned_keys:
        print(key.table, key.provisional, "->", key.assigned)
        # tasks {'id': 4503599627370497} -> {'id': 901}
```

A placeholder is recognised by tracking which keys the sandbox itself
generated, never by size, so an ordinary large integer -- a snowflake id, a
hash -- is just a value. A `TEMP` trigger records any key the agent writes
itself into the provisional column.

Three things are still refused, with `GeneratedValueError`:

- **The agent supplying the key itself.** That bypasses the sequence, so a later
  ordinary insert can collide with it.
- **A row referencing a key the sandbox generated.** The parent's real key is
  only known once it is written, and SafeAgentDB will not guess it. This covers
  a foreign-key column pointing at a provisional key, and any other column
  holding exactly a key the sandbox generated. Insert the parent through the
  application first, then let the agent reference its real key.
- **A row deleted and re-inserted under the same custom `row_key`.** That reads
  as an `UPDATE` carrying a new placeholder in the generated column, which
  production would store as a real id.

A generated **non-key** column (a `gen_random_uuid()` default, say) is refused
only when the agent leaves it empty; supplying a value is fine.

#### Reference tables

The clone is tenant-scoped, so a shared lookup table with no tenant column
clones **zero rows**. A foreign key pointing at it could never be satisfied, so
SafeAgentDB drops that constraint in the sandbox and says so in
`unsupported_constraints`:

```
tasks: FOREIGN KEY (status_id) -> statuses not enforced -- the parent table
cloned 0 rows, so every reference would look dangling. If statuses holds no
tenant data, pass reference_tables=['statuses'] to clone it in full.
```

Listing the table restores the constraint:

```python
with ShadowDB(
    prod_engine,
    tables=["tasks"],
    tenant_id=42,
    reference_tables=["statuses"],       # cloned in full, tenant filter ignored
) as sandbox:
    ...
```

Two things to know before you list a table:

- **Every row becomes visible to the agent.** Only list tables whose whole
  contents any tenant may see. A table holding tenant data must never be listed.
- **They are read-only.** Reference tables are excluded from the changeset, and
  modifying one raises `SyncError` from `diff()` rather than failing later.

**Context Manager Lifecycle:**

| Phase | What Happens |
|-------|-------------|
| `__enter__` | 1. Reflects schema from production and converts `tenant_id` to the tenant column's type. 2. Creates an in-memory SQLite sandbox with foreign keys enforced, carrying constraints, unique indexes, column defaults and referential actions across (on a SQLite production the actions are read from `PRAGMA foreign_key_list`, since SQLAlchemy's inspector omits them). 3. Resolves a row key per table, raising `SchemaError` if one is missing. 4. Clones only rows where `tenant_column = tenant_id`. 5. Snapshots the cloned state for later diffing, installs the key-change tracking triggers and records the schema. 6. Opens a SQLAlchemy `Session`. Any database error -- an unknown table, an unreachable server -- raises `SchemaError` with the driver's error as `__cause__`. |
| *inside `with`* | AI operates freely on the sandbox via `execute()`, `query()`, or `session`. Schema changes (DDL) by the agent make `diff()` and the commit raise `SchemaError`. |
| `__exit__` | Session closed. Sandbox engine disposed. All in-memory data destroyed. |

**Re-entry.** A `ShadowDB` that exited without committing can be entered again;
each entry takes a fresh clone and the earlier sandbox's work is gone. Entering
one that is already open raises `UsageError` (the open sandbox is left as it
is), and so does entering one that has committed.

**Methods:**

| Method | Signature | Description |
|--------|-----------|-------------|
| `execute` | `(sql: str, params: dict \| None) -> CursorResult` | Execute raw SQL inside the sandbox and return a standard SQLAlchemy `CursorResult`. **Without `params`** the string goes to SQLite exactly as written, so a literal such as `'{"a":1}'` is never read as a bind parameter. **With `params`** it runs as a `text()` statement with `:name` placeholders; `Decimal`, `datetime`, `date`, `time` and `UUID` values are bound as text. A failing statement raises `AgentSQLError` (driver error as `__cause__`); everything the agent did before it is kept. |
| `query` | `(sql: str, params: dict \| None) -> list[dict]` | Execute a statement that returns rows and return them as plain dictionaries. A statement that returns no rows raises `AgentSQLError` -- it has still run, so use `execute()` for writes. |
| `diff` | `() -> ChangeSet` | Flushes pending changes, snapshots the current sandbox state, and computes a row-level diff against the original clone. Returns a `ChangeSet` object. Raises `SchemaError` if the agent changed the sandbox schema, `SyncError` if it changed a reference table, and `UsageError` after a successful commit (the committed changes would otherwise be shown for approval again). |
| `commit_to_production` | `(changeset: ChangeSet \| None = None) -> int` | Pass the `ChangeSet` you reviewed and the commit refuses with `ChangesetMismatchError` if the sandbox changed since -- **the recommended form wherever anything approves the change**. Omit it to commit whatever the sandbox currently holds. Runs all 7 safety gates and syncs approved changes to production in one atomic transaction, writing only the columns the agent changed. Returns the number of rows **SafeAgentDB's own statements wrote**, summed from each statement's rowcount; rows production changed through a referential action you allowed with `allow_cascades=True` are not counted. Raises `CascadeError`, `ChangesetMismatchError`, `ConflictError`, `GeneratedValueError`, `IntegrityViolationError`, `MissingValidatorError`, `RowValidationError`, `SchemaError` or `SyncError` -- see [Errors](#errors) -- and `UsageError` outside the `with` block or when `changeset` is not a `ChangeSet`. Can only be called once per sandbox (double-commit raises `SyncError`). |

**Properties:**

| Property | Type | Description |
|----------|------|-------------|
| `clone_stats` | `dict[str, int]` | Number of rows cloned per table when the sandbox was created. |
| `tables` | `list[str]` | Table names available in this sandbox. |
| `dialect` | `str` | Production database dialect name (`'postgresql'`, `'mysql'`, `'sqlite'`). |
| `row_keys` | `dict[str, list[str]]` | The row-identifying columns in use for each cloned table. |
| `reference_table_names` | `list[str]` | Tables cloned in full and treated as read-only. |
| `generated_columns` | `dict[str, list[str]]` | Columns whose production-side generated default the sandbox could not reproduce. |
| `provisional_key_columns` | `dict[str, list[str]]` | Key columns the sandbox fills with a placeholder for production to replace. See [Generated keys](#generated-keys). |
| `assigned_keys` | `list[AssignedKey]` | For the last commit, the real keys production assigned to rows that held a placeholder. |
| `skipped_conflicts` | `list[SkippedConflict]` | Rows the last commit left unapplied under `on_conflict="ignore"`. Empty otherwise. |
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
| `validate_row(table_name, row_data, *, require_validator=True)` | Validates a dict against the registered model. With `require_validator=True` (the default) a missing validator raises `MissingValidatorError`; with `False` it warns and returns `None`. Raises pydantic's `ValidationError` on bad data -- this low-level helper is not wrapped; the commit wraps it in `RowValidationError`. |
| `format_validation_error(exc)` | Every failing field of a pydantic `ValidationError` with its reason and the value it got, as used in the dashboard and in `RowValidationError`. |
| `missing_validator_message(table_name)` | The single wording used by both `RowDiff.validate()` and `validate_row()`, so the dashboard and the commit can never disagree. |

### `ChangeSet`

Returned by `sandbox.diff()`. Contains the full set of row-level changes.

| Member | Type | Description |
|--------|------|-------------|
| `diffs` | `list[RowDiff]` | Raw list of individual row changes. |
| `is_empty` | `bool` | `True` if the AI made no changes. |
| `is_valid` | `bool` | `True` when nothing SafeAgentDB can check without production rows is wrong: per-row validation plus every `blocking_errors` entry. Stricter in 0.3.0 -- a tenant breach, a duplicate row key, a write to a reference table, a key change or a cycle between rows now make it `False`, where 0.2.x reported those only at commit or not at all. A clean `is_valid` still does not promise the commit will succeed: drift, uniqueness against other tenants' rows, and referential actions on rows that were not cloned need production. |
| `unsupported_constraints` | `list[str]` | Schema elements not enforced in the sandbox, carried through from `ShadowDB` so the rendered diff can warn about them. |
| `blocking_errors` | `list[str]` | Problems the commit will refuse that were found without touching production: a tenant breach, a row key that no longer identifies a row, a row key the agent changed, a write to a reference table, rows that reference each other in a cycle. Any entry makes `is_valid` `False`. |
| `fingerprint` | `str` | Hash of what this changeset would write, over a canonical form that tags every value with its type, so values of different types (`1`, `True`, `1.0`, `"1"`; `b'x'` and the text `"b'x'"`) never hash alike. Pass the changeset to `commit_to_production()` and it is compared, so only what was reviewed can land. |
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
| `changed_columns()` | `-> list[str]` | Column names that differ between `old` and `new` (UPDATEs only), compared by type as well as value: `1` to `true` in a JSON value is a change, a JSON or timestamp value that is only formatted differently is not. |
| `logical_row(row)` | `-> dict` | A row as production will hold it: JSON decoded, timestamps parsed. What the `SafeModel` sees. |
| `require_validator` | `bool` | Whether a missing `SafeModel` makes this row invalid. Set from `ShadowDB(require_validators=...)`. |
| `validate()` | `-> tuple[bool, str]` | Runs Pydantic validation on this row. Returns `(is_valid, message)`. Treats a missing validator exactly as the commit will. |

### Errors

All exceptions live in `safeagentdb.errors` and are importable from
`safeagentdb`. Every one derives from `SafeAgentDBError`, so a whole agent
session can be wrapped in a single `except`.

```
SafeAgentDBError
|-- SchemaError                 production schema cannot be sandboxed safely,
|                               or the agent changed the sandbox schema
|-- SyncError                   a changeset was rejected or could not be applied
|   |-- ConflictError           production drifted between clone and commit
|   |-- CascadeError            a referential action would act on unseen rows
|   |-- ChangesetMismatchError  the sandbox changed since the review
|   |-- DuplicateRowKeyError    the row key does not identify one row
|   |-- GeneratedValueError     a row needs a value only production can generate
|   |-- IntegrityViolationError production rejected a row the sandbox accepted
|   +-- RowValidationError      a row failed its SafeModel at commit
|-- AgentSQLError               a statement the agent ran in the sandbox failed
|-- UsageError                  the API was misused (also a RuntimeError)
+-- MissingValidatorError       no SafeModel registered and one is required
```

Every exception a public method raises derives from `SafeAgentDBError`: errors
while `__enter__` reads production, every database error during a commit
(starting the transaction, the reads before an `INSERT`, the cascade check, the
conflict diagnosis, bind-time errors), the agent's own failed statements,
commit-time validation failures and misuse. Where a driver or pydantic error was
the cause, it is kept as `__cause__`. The one documented exception is the
low-level `safeagentdb.models.validate_row()` helper, which raises pydantic's
`ValidationError` directly.

| Exception | Raised when |
|-----------|-------------|
| `SchemaError` | A cloned table has no primary key and no usable `row_key`; a `row_key` names missing columns or is not unique in the cloned rows; the production schema cannot be reproduced in SQLite; `__enter__` hit a database error (unknown table, unreachable server); the tenant column holds different types in different tables; the agent changed the sandbox schema or dropped the tracking triggers. |
| `SyncError` | Anything `diff()` reports as blocking (tenant breach, changed row key, cycle between rows, write to a reference table); an `UPDATE`/`DELETE` has no row-identifying key; a value production's type cannot take (text in a JSON column that is not JSON, text in a datetime column that is not a timestamp); a database error during the commit; the sandbox is committed twice. |
| `ConflictError` | A guarded `UPDATE`/`DELETE` matched nothing, or a row key matches more than one row. Carries `.table`, `.row_key` and `.columns` (the drifted column names). The message only blames another writer when re-reading the row shows a column that changed; otherwise it says it cannot name one, and if an earlier statement's referential action (under `allow_cascades=True`) could have changed the row, it says that. |
| `GeneratedValueError` | A new row supplies a key production is meant to assign, needs a generated value the sandbox could not hold for it, references a key the sandbox generated, or was deleted and re-inserted under the same custom `row_key`. Carries `.table` and `.columns`. See [Generated keys](#generated-keys). |
| `IntegrityViolationError` | Production rejected a row the sandbox accepted. The message gives the driver's reason and the possible causes without claiming one: a row outside the tenant or in a table that was not cloned, or the unit-of-work order not suiting the changeset (a unique value moved from a deleted row to a new one). Carries `.table` and `.row_key`, and the driver's `IntegrityError` as `__cause__`. |
| `RowValidationError` | A row failed its `SafeModel` at commit. `diff()` reports the same row as failed first. Carries `.table`, `.row_key`, `.errors` (pydantic's structured errors) and pydantic's `ValidationError` as `__cause__`. |
| `MissingValidatorError` | A table has no registered `SafeModel` and `require_validators` is `True`. Also subclasses `KeyError`, so 0.1.x handlers keep working. |
| `DuplicateRowKeyError` | A row key does not identify a single row: two sandbox rows share it, or an INSERT carries a key production already holds. Carries `.table` and `.row_key`. |
| `CascadeError` | A `DELETE`, or an `UPDATE` to a referenced column, would make production run a referential action on rows the diff does not show: rows of this tenant (unless `allow_cascades=True`), rows of another tenant, or rows with no determinable owner. Carries `.table`, `.referencing_table`, `.action`, and `.tenants` (other tenants) or `.unattributed_rows` (rows whose referencing table has no tenant column, or whose tenant value is `NULL`). |
| `ChangesetMismatchError` | `commit_to_production(changeset=...)` was given a changeset the sandbox no longer matches. Carries `.reviewed` and `.current` fingerprints. |
| `AgentSQLError` | A statement passed to `execute()` or `query()` failed, or `query()` was given a statement that returns no rows. The sandbox keeps everything done before it. |
| `UsageError` | A method was called outside the `with` block; `diff()` was called after a successful commit; an open or committed `ShadowDB` was entered again; `on_conflict` or `changeset` has the wrong value; `tenant_id` does not convert exactly to the tenant column's type. Subclasses `RuntimeError`, which these cases raised before 0.3.0 (a bad `on_conflict` raised `ValueError`). |
| `MissingValidatorWarning` | Not an error: warned when `require_validators=False` and a row is written unvalidated. |
| `ConflictWarning` | Not an error: warned once per row skipped under `on_conflict="ignore"`. |

**Handling a conflict:**

```python
from safeagentdb import ConflictError

try:
    sandbox.commit_to_production()
except ConflictError as exc:
    print(f"{exc.table} row {exc.row_key} drifted on {exc.columns}")
    # Nothing was written. Re-clone and let the agent try again.
```

**Partially applying instead**, with `on_conflict="ignore"`:

```python
with ShadowDB(engine, tables=["tasks"], tenant_id=42, on_conflict="ignore") as sandbox:
    ...
    written = sandbox.commit_to_production()   # fewer than the changeset held

    for skip in sandbox.skipped_conflicts:     # never silent
        print(f"skipped {skip.table} {skip.row_key}: {skip.columns} drifted")
```

Each skipped row also raises a `ConflictWarning`, so a partial apply is visible
even when nobody inspects the result.

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
# --> SyncError: "This changeset cannot be committed:
#       - Tenant breach blocked on INSERT: row in 'tasks' has user_id=777, expected 42."

# Scenario 2: AI tries to UPDATE a row to change its tenant
sandbox.execute("UPDATE tasks SET user_id = 777 WHERE id = 1")
sandbox.commit_to_production()
# --> SyncError: "This changeset cannot be committed:
#       - Tenant breach blocked on UPDATE: row in 'tasks' has user_id=777, expected 42."

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

The sandbox is always in-memory SQLite. What differs is how well each
**production** database is supported:

| Production | Support | What that means |
|------------|---------|-----------------|
| **PostgreSQL** | **First-class** | The production target SafeAgentDB is designed and tested for. CI runs the server-backed suite (`tests/test_server_backed.py`) against PostgreSQL 16: tenant isolation, compare-and-swap against a concurrent transaction, sequences, foreign keys and their order, cascades, key changes, `JSONB`/`ARRAY` and `timestamptz` round-trips. `JSONB`, `JSON`, `ARRAY` and `HSTORE` are carried as JSON text; `UUID`, `INET`, `CIDR`, `MACADDR`, `CITEXT`, `TSVECTOR` as text. |
| **SQLite** | **Best-effort** | Exercised by most of the suite, which uses SQLite as the stand-in production, and the issues that were cheap to fix are fixed. The known limits are listed under [SQLite production](#sqlite-production). |
| **MySQL / MariaDB** | **Untested** | Nothing in the library is MySQL-specific and every production statement is built from SQLAlchemy Core, so identifiers are quoted by the dialect. `TINYINT`/`MEDIUMINT` are integers in the sandbox; `ENUM`, `SET` and `YEAR` are carried as text. No test runs against a MySQL server, so treat this as unverified. |
| Others | Not supported | Other SQLAlchemy dialects may work, but nothing is known about them. |

The production sync always uses the original production metadata; the type
mapping only applies to the throwaway sandbox. Values are carried through it
as follows:

- **JSON** (`JSON`, `JSONB`, `ARRAY`, `HSTORE`): every value is held as its JSON
  encoding, scalars included, and decoded back at commit. The string `"123"`,
  the number `123` and `true` stay distinct. Text the agent writes into such a
  column must be JSON (`'"hello"'` for a string); anything else is refused
  rather than guessed at. SQL `NULL` and JSON `null` are the same value in the
  sandbox, as in SQLAlchemy's own `JSON` type.
- **Datetimes**: held as ISO 8601 text, which keeps a UTC offset. A
  `timestamptz` keeps its instant; on a SQLite production the stored text is
  read as it is and an offset is written back with the value.
- **Text-mapped types** (`UUID`, `INET`, ...): held and written back as text.

#### SQLite production

Best-effort. Fixed for 0.3.0: referential actions are read from
`PRAGMA foreign_key_list` and carried into the sandbox, and datetime offsets
survive. Known limits:

- `DATETIME`, `DATE`, `TIME` and `BOOLEAN` columns are **left out of the
  compare-and-swap guard**. SQLite stores them as text or numbers in whatever
  format the writer chose (`'2024-01-01 10:00:00'`, `'2024-01-01T10:00:00'`,
  `'true'`, `1`) and compares that storage literally, so a guard on them failed
  with nothing changed -- including for SQLite's own `CURRENT_TIMESTAMP`
  format. A concurrent edit confined to such a column is therefore not
  detected on SQLite. The same columns are guarded on PostgreSQL.
- A datetime written back is stored in SQLAlchemy's text format plus the
  offset (`'2024-01-01 10:00:00.000000+02:00'`), so a value copied from a row
  stored as `'2024-01-01T10:00:00Z'` lands as the same instant in a different
  format.
- SQLite's dynamic typing means a value the sandbox accepts is accepted by a
  SQLite production too, whatever its declared type; nothing beyond your
  `SafeModel` checks the type.

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
- **Indirect writes through referential actions**: the tenant filter applies to
  the statements SafeAgentDB issues. A foreign key declared `ON DELETE CASCADE`,
  `SET NULL` or `SET DEFAULT` (or the `ON UPDATE` forms) makes the database act
  on further rows *by itself*, and no `WHERE` clause of ours constrains those.
  Every such key pointing into a cloned table is found at `__enter__` -- across
  all of production, not just the cloned tables -- reported in
  `unsupported_constraints`, and checked at commit, inside the transaction:
  if the action would act on any existing row, `CascadeError` rolls the
  changeset back, because that effect is not in the diff. `allow_cascades=True`
  permits it within the tenant. **Another tenant's rows, and rows with no
  determinable owner, are refused either way**: an audit log keyed only on
  `task_id` cannot be attributed to anyone, and an unknown owner is not treated
  as the acting tenant. When the referencing table is cloned, the sandbox runs
  the action itself, so the diff shows its effect and the commit needs no
  opt-in.
- **Key changes**: an `UPDATE` that changes a row key is refused, because it
  would reach production as `DELETE` + `INSERT` and run the wrong referential
  actions. See [Key changes](#key-changes).
- **Exactly what was reviewed**: `commit_to_production(changeset=...)` compares
  the changeset's fingerprint against a freshly computed diff and refuses if the
  sandbox moved on, so an approval cannot be widened after the fact. The
  fingerprint tags every value with its type. A change Python considers equal
  (`1` to `true` inside a JSON value) is detected and written; a value only
  formatted differently is not a change.
- **Production drift**: a row changed by another process between clone and
  commit is a `ConflictError`, not a silent overwrite. The clone-time values are
  carried in the `WHERE` clause of the `UPDATE`/`DELETE` itself, so the check and
  the write are one statement. **This holds at any isolation level**, including
  PostgreSQL `READ COMMITTED` and MySQL `REPEATABLE READ`, and takes no row
  locks. An `UPDATE` guards the columns the agent changed, so an unrelated
  concurrent edit to a different column of the same row is allowed to coexist; a
  `DELETE` guards the whole row. On PostgreSQL this is verified in CI; on a
  SQLite production some column types are left out of the guard (see
  [SQLite production](#sqlite-production)).
- **Values only production can generate**: a `serial`/identity key is left for
  production's sequence to assign and reported back, never invented by the
  sandbox. Supplying one by hand, or pointing at a placeholder that does not
  exist yet, is refused with `GeneratedValueError`.
- **Statement order**: INSERTs parents first, then UPDATEs, then DELETEs
  children first, with rows of the same table (an org chart, a comment thread)
  ordered by their references. Re-pointing a child and then deleting its old
  parent commits. Rows that reference each other in a cycle have no order
  production would accept one statement at a time, and are refused in
  `diff()`.

### What the sandbox cannot catch

- **Anything listed in `unsupported_constraints`.** Dialect-specific column
  types are stored as `TEXT`/`VARCHAR`, so a malformed UUID passes in the
  sandbox and is rejected by production. Server defaults with no SQLite
  equivalent, foreign keys pointing outside the cloned tables, referential
  actions whose referencing table was not cloned, and `CHECK` constraints that
  reflection could not reproduce are all listed there.
- **The shape of a cascade into a table that was not cloned.** The commit
  refuses it (see above), but cannot show you the affected rows in the diff.
  Clone the referencing table to see them.
- **The validity rules of remapped columns.** JSON and datetime values are
  carried through the sandbox as text and converted back exactly (see
  [Supported Databases](#supported-databases)), but the sandbox does not
  enforce, say, an `ARRAY` element type, and JSON and text-mapped columns are
  excluded from the compare-and-swap guard because the two sides hold
  different representations.
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
- **Concurrency beyond the rows being written.** The compare-and-swap guard
  covers the rows in the changeset. A row the agent merely *read* is not
  guarded, so a decision based on stale data can still be applied. Nor is a row
  inserted by someone else in the meantime: uniqueness against it surfaces as
  `IntegrityViolationError` at commit, not as a conflict.
- **Columns whose equality is unreliable.** `Float`/`REAL`/`DOUBLE`, `JSON`,
  `JSONB`, `ARRAY`, `HSTORE`, `TSVECTOR`, `MONEY` and binary columns are
  **excluded from the guard**: their values do not survive a driver round-trip
  reliably enough to gate a write on, and comparing them would reject correct
  changesets. A concurrent edit confined to such a column is not detected. On a
  SQLite production, `DATETIME`, `DATE`, `TIME` and `BOOLEAN` are excluded too.
- **Changesets the unit-of-work order does not suit.** INSERTs run before
  DELETEs, so moving a unique value from a row the agent deleted to a row it
  inserted fails in production with `IntegrityViolationError` (nothing is
  written). Commit the delete first, in its own changeset.
- **Key changes.** Refused, never translated: change keys through the
  application.
- **An `ON UPDATE` action on a cloned child, with `allow_cascades=True`.** The
  parent's `UPDATE` runs first and production updates the children itself, so
  the children's own `UPDATE`s then match nothing and the commit refuses with a
  `ConflictError` that names the earlier referential action. Without
  `allow_cascades` it is refused up front with `CascadeError`.
- **Unguarded columns on an UPDATE.** By design only the changed columns are
  guarded, so a concurrent edit to a column the agent did not touch is allowed
  through. That is the point -- it keeps unrelated work from colliding -- but it
  does mean the row as a whole is not frozen between clone and commit.
- **Scale.** The entire tenant scope is copied into memory. This is designed for
  one tenant's working set, not for a full-table migration.

- **A new row referencing another new row.** Refused rather than resolved. If
  your agent needs to build a parent and its children in one go, create the
  parent through the application first.

### Verified against a real server

The sandbox is SQLite, so most of the suite uses SQLite as the stand-in
production database. The claims that only a real server can settle --
compare-and-swap against a genuinely concurrent transaction, sequences assigning
keys, foreign keys and statement order, cascades and key changes, `JSONB` and
`timestamptz` round-trips, tenant isolation -- are covered by
`tests/test_server_backed.py`, which CI runs against a **PostgreSQL 16** service
container. They are skipped locally unless you set `DATABASE_URL`:

```bash
DATABASE_URL=postgresql+psycopg2://user:pass@localhost/db python -m pytest -m requires_db
```

MySQL is not covered; see [Supported Databases](#supported-databases).

### Operational notes

- The sandbox is **always in-memory SQLite**, whatever your production dialect.
- `MetaData.reflect` resolves foreign keys, so asking for one table may clone the
  tables it references as well. Check `clone_stats` to see what was copied.
- Foreign key enforcement is suspended while the tenant's rows are loaded, since
  a tenant-scoped clone is a partial view and may reference parents that were
  not copied. It is on for everything the agent does afterwards.
- `commit_to_production()` can be called once per sandbox, and `diff()` raises
  `UsageError` after it.
- The sandbox's own tracking objects are `TEMP` tables and triggers named
  `_safeagentdb_*`. They live in SQLite's temp schema, never reach a diff, and
  are read-only to the agent.

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
sandbox.commit_to_production(changeset)
  |
  |-- 1. Flush the sandbox session; refuse if the agent changed the schema
  |                                                       --> SchemaError
  |-- 2. Snapshot the sandbox and read the key-change log
  |-- 3. Compute the row-level diff vs the clone, keyed on the row key,
  |      comparing values by type (canonical form)
  |-- 4. Compare fingerprints with the reviewed changeset --> ChangesetMismatchError
  |-- 5. Refuse blocking errors: tenant breach, changed row key, row cycle,
  |      reference-table write, ambiguous row key          --> SyncError
  |
  |-- 6. Order: INSERTs (parents first, rows by their references),
  |             UPDATEs (parents first),
  |             DELETEs (children first, rows by their references)
  |
  |-- Inside ONE engine.begin() transaction, FOR EACH diff:
  |     |-- 7. Tenant matches in row data                  --> SyncError
  |     |-- 8. Written JSON/datetime values decode         --> SyncError
  |     |-- 9. SafeModel validation                        --> RowValidationError
  |     |                                                     MissingValidatorError
  |     |-- 10. Generated-key checks                       --> GeneratedValueError
  |     |-- 11. INSERT? refuse a key production holds      --> DuplicateRowKeyError
  |     |       insert(table).values(row, provisional key left out)
  |     |
  |     |-- 12. Row key present and correct                --> SyncError
  |     |-- 13. Referential actions: count referencing rows per tenant
  |     |       other tenant / unknown owner               --> CascadeError
  |     |       this tenant, without allow_cascades        --> CascadeError
  |     |
  |     '-- 14. One compare-and-swap statement:
  |           |-- UPDATE: update(table)
  |           |             .where(row key AND tenant_id
  |           |                    AND each changed, guardable column = its clone value)
  |           |             .values(only the changed columns)
  |           |-- DELETE: delete(table)
  |           |             .where(row key AND tenant_id
  |           |                    AND every guardable column = its clone value)
  |           |-- rowcount == 1 --> applied
  |           |-- rowcount == 0 --> re-read to explain --> ConflictError
  |           '-- rowcount > 1  --> the key is not unique --> ConflictError
  |
  |-- Any database error, at any step                    --> SyncError (cause kept)
  '-- Any failure --> full rollback, zero writes
```

A `NULL` clone-time value becomes `col IS NULL` rather than `col = NULL`, which
never matches. Because the guard value is a Python value rather than a bind
parameter, that branch is exact on every dialect and needs no
`IS NOT DISTINCT FROM` or `<=>` handling.

Every statement sent to production -- the writes, the existing-key read, the
cascade check and the conflict diagnosis -- is built from SQLAlchemy Core
constructs (`insert()`, `update()`, `delete()`, `select()`), so identifiers are
quoted by the dialect and values are always bound. The only literal SQL is
inside the sandbox: SQLite `PRAGMA`s and the tracking triggers, built from
schema identifiers quoted by SQLite's own preparer.

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
|-- sync.py         Atomic production sync: tenant guards, row-key guard, compare-and-swap
'-- py.typed        PEP 561 type checker marker

tests/
|-- conftest.py              Fixtures, including the server engine for DATABASE_URL
|-- test_core.py             End-to-end release check (script style)
|-- test_mega.py             Exhaustive coverage of the public API
|-- test_weaknesses.py       Regression guards for the four findings in docs/AUDIT.md
|-- test_hardening.py        The machinery added in 0.2.0
|-- test_audit_followups.py  Five issues found reviewing that work
|-- test_review_findings.py  The six findings of the second review (docs/AUDIT.md)
|-- test_review_0_3.py       The findings of the pre-0.3.0 review (docs/REVIEW_0_3.md)
'-- test_server_backed.py    Claims only a real server can settle (PostgreSQL in CI)

docs/
|-- AUDIT.md         The weakness audit and the second review
|-- REVIEW_0_3.md    The pre-0.3.0 review, with the status of each finding
'-- review-repros/   Runnable reproductions for REVIEW_0_3.md, kept as they were run
```

Run the suite with `python -m pytest -q` and the linter with `ruff check .`;
both run in CI on Python 3.10, 3.11 and 3.12, and the server-backed tests run
against PostgreSQL 16.

## License

MIT -- see [LICENSE](LICENSE).
