# SafeAgentDB

**Shadow-Sandbox DB Layer for AI Agents.**

Let AI modify your production database without the terror. SafeAgentDB intercepts every AI-generated write, isolates it in an in-memory sandbox, validates it with Pydantic, shows you a diff, and only syncs on your explicit approval — in a single atomic transaction.

```
Production DB ──► ShadowDB clones tenant rows ──► In-Memory SQLite Sandbox
                                                          │
                                                   AI operates freely
                                                          │
                                                   Diff + Validate
                                                          │
                                              approve? ──► Atomic Sync
```

---

## Why SafeAgentDB?

You're building AI-powered features — an agent that updates tasks, a copilot that manages billing, an assistant that edits user profiles. But two things keep you up at night:

**1. AI Logical Errors**
An LLM sets `status = 'yolo_swag'` instead of `'done'`. Or it writes `-500` to a balance field. Without a safety net, that goes straight to production.

**2. Multi-Tenancy Breach**
Your AI agent operates on behalf of User 42 but accidentally updates User 99's rows. One misplaced WHERE clause and you have a data breach.

SafeAgentDB eliminates both risks. Every AI operation is sandboxed, validated, diffed, and tenant-scoped — before it touches production.

---

## The 4 Safety Gates

Every `commit_to_production()` call passes through 4 sequential gates. If **any** gate fails, the **entire** transaction rolls back. Nothing is written.

| Gate | What It Does | Catches |
|------|-------------|---------|
| **1. Tenant Isolation at Clone** | Only rows matching `tenant_id` are copied into the sandbox. Other tenants' data never enters memory. | AI accessing wrong user's data |
| **2. Row-Level Diffing** | Changes are computed as explicit INSERT/UPDATE/DELETE ops with before/after values. You see exactly what the AI changed. | Silent data corruption |
| **3. Pydantic Re-Validation** | Every row is validated against your `SafeModel` schema. Strict mode, no type coercion. | Invalid enums, wrong types, missing fields |
| **4. Atomic Sync + Tenant WHERE** | Entire changeset runs in one transaction. Every UPDATE/DELETE includes `WHERE tenant_id = ?` regardless of what the AI wrote. | Partial writes, tenant escape at SQL level |

---

## Quick Start

### Install

```bash
pip install safeagentdb
```

### Define a validator

```python
from typing import Literal
from safeagentdb import SafeModel

class TaskValidator(SafeModel):
    __table_name__ = "tasks"

    id: int
    user_id: int
    title: str
    status: Literal["todo", "in_progress", "done"]
```

### Sandbox an AI agent

```python
from sqlalchemy import create_engine
from safeagentdb import ShadowDB

prod_engine = create_engine("postgresql://user:pass@localhost/mydb")

with ShadowDB(prod_engine, tables=["tasks"], tenant_id=42) as sandbox:
    # AI does its thing — full CRUD, raw SQL, whatever
    sandbox.execute("UPDATE tasks SET status = 'done' WHERE id = 1")
    sandbox.execute(
        "INSERT INTO tasks (id, user_id, title, status) "
        "VALUES (99, 42, 'New task from AI', 'todo')"
    )

    # Review the diff
    print(sandbox.diff().display())

    # Only if you approve:
    sandbox.commit_to_production()
```

### What the diff looks like

```
SafeAgentDB ChangeSet: 1 insert(s), 1 update(s), 0 delete(s)

Table | Op     | PK | Column  | Old  | New              | Validation
------+--------+----+---------+------+------------------+-----------
tasks | INSERT | 99 | id      | -    | 99               | PASS
      |        |    | user_id | -    | 42               |
      |        |    | title   | -    | New task from AI |
      |        |    | status  | -    | todo             |
tasks | UPDATE | 1  | status  | todo | done             | PASS
------+--------+----+---------+------+------------------+-----------
  >> ALL VALIDATIONS PASSED
```

### When validation fails

```python
# AI writes garbage
sandbox.execute("UPDATE tasks SET status = 'yolo_swag' WHERE id = 1")

changeset = sandbox.diff()
print(changeset.display())
# >> VALIDATION FAILURES DETECTED

sandbox.commit_to_production()  # Raises pydantic.ValidationError
# Production is untouched.
```

---

## API Reference

### `ShadowDB(prod_engine, tables, tenant_id, tenant_column="user_id")`

The main context manager. Accepts any SQLAlchemy `Engine`.

| Method | Description |
|--------|-------------|
| `sandbox.execute(sql, params)` | Execute raw SQL in the sandbox. Returns `CursorResult`. |
| `sandbox.query(sql, params)` | Execute a SELECT, return `list[dict]`. |
| `sandbox.diff()` | Compute and return a `ChangeSet` with visual diff. |
| `sandbox.commit_to_production()` | Validate + sync atomically. Returns rows affected. |
| `sandbox.clone_stats` | `dict[str, int]` — rows cloned per table. |
| `sandbox.dialect` | Production DB dialect name (`'postgresql'`, `'mysql'`, etc.). |
| `sandbox.session` | Raw SQLAlchemy `Session` for ORM-style operations. |

### `SafeModel`

Pydantic v2 base model with `strict=True` and `extra="forbid"`. Set `__table_name__` on subclasses to auto-register validators.

### `ChangeSet`

| Property/Method | Description |
|----------------|-------------|
| `.display()` | CLI-formatted diff table with validation status. |
| `.is_empty` | `True` if no changes detected. |
| `.is_valid` | `True` if all rows pass Pydantic validation. |
| `.summary` | `{"INSERT": n, "UPDATE": n, "DELETE": n}` |
| `.diffs` | `list[RowDiff]` — raw diff objects. |

---

## Supported Databases

SafeAgentDB works with **any database that SQLAlchemy supports** as the production source. The sandbox is always in-memory SQLite (for speed and zero-config isolation).

| Database | Production Source | Sandbox Target | Status |
|----------|------------------|----------------|--------|
| **PostgreSQL** | Yes | Auto-mapped | Fully supported |
| **MySQL / MariaDB** | Yes | Auto-mapped | Fully supported |
| **SQLite** | Yes | Direct clone | Fully supported |
| **Microsoft SQL Server** | Yes | Auto-mapped | Supported via SQLAlchemy |
| **Oracle** | Yes | Auto-mapped | Supported via SQLAlchemy |

**Dialect-aware type mapping**: PostgreSQL types like `JSONB`, `UUID`, `ARRAY`, `INET`, `HSTORE`, and `TSVECTOR` are automatically mapped to SQLite-safe equivalents in the sandbox. The production sync always uses the original metadata — zero fidelity loss.

---

## How Sync Works Internally

```
sandbox.commit_to_production()
    │
    ├─ 1. Flush sandbox session
    ├─ 2. Snapshot current sandbox state
    ├─ 3. Compute row-level diff vs original clone
    │
    ├─ FOR EACH diff:
    │   ├─ 4. Assert tenant_id matches in row data      ← SyncError if breach
    │   ├─ 5. Run Pydantic model_validate(row)          ← ValidationError if bad
    │   └─ 6. Build SQLAlchemy Core statement:
    │       ├─ INSERT: insert(table).values(row)
    │       ├─ UPDATE: update(table).where(pk AND tenant_id).values(row)
    │       └─ DELETE: delete(table).where(pk AND tenant_id)
    │
    └─ 7. All statements execute inside engine.begin()   ← Single atomic transaction
         └─ Any failure → full rollback, zero writes
```

No raw SQL is generated. Every statement uses SQLAlchemy Core constructs, making the sync engine dialect-agnostic and injection-safe.

---

## Architecture

```
safeagentdb/
├── __init__.py     # Public API: ShadowDB, SafeModel, ChangeSet, SyncError
├── engine.py       # Schema reflection, type mapping, sandbox creation
├── sandbox.py      # ShadowDB context manager
├── models.py       # SafeModel base + auto-registration registry
├── diff.py         # Row-level diff engine + CLI table renderer
└── sync.py         # Atomic production sync with tenant guards
```

---

## License

MIT
