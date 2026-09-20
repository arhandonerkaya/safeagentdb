# SafeAgentDB — Weakness Audit

Date: 2026-09-20 · Version audited: 0.1.2 · Branch: `audit`

Verification only — no library code was changed. Evidence lives in
[tests/test_weaknesses.py](../tests/test_weaknesses.py).

Each claim has `test_observed_*` tests that assert the behaviour the library has
**today** (these pass — they are the proof) and one `test_expected_*` test that
asserts the behaviour a safe library should have. The latter are marked
`xfail(strict=True)`: they fail today, and will hard-fail the moment the bug is
fixed, prompting removal of the marker so they become regression guards. Run
`pytest --runxfail` to see them as ordinary failures.

**Suite result:** 88 passed, 4 xfailed, 1.6 s.

---

## 1. Schema fidelity — CONFIRMED for non-SQLite production, NOT CONFIRMED for SQLite

**Code path.** [engine.py:127-151](../safeagentdb/engine.py#L127-L151)
`clone_schema_to_sandbox()` branches on
[`_detect_dialect()`](../safeagentdb/engine.py#L193-L202):

- dialect ≠ `sqlite` → [`_clone_table_for_sqlite()`](../safeagentdb/engine.py#L92-L106),
  which rebuilds each table from `Column(name, type, primary_key, nullable)` only;
- dialect = `sqlite` → `table.to_metadata()`, which copies the table whole.

**What the tests do.** A Postgres `MetaData` is built in memory (`users` with
`UNIQUE(email)`, `CHECK(age >= 0)`, `plan` defaulting to `'free'`, a `JSONB`
column; `tasks` with a FK to `users.id`) and cloned into a sandbox engine. The
same logical schema is also created as a real SQLite file DB in `tmp_path` and
cloned via `reflect_tables()`.

**Observed behaviour.**

| | Postgres-typed MetaData | SQLite production DB |
|---|---|---|
| Constraints on sandbox table | `{PrimaryKeyConstraint}` only | PK + UNIQUE + CHECK + FK |
| `server_default` | dropped | preserved (`DEFAULT 'free'`) |
| Duplicate UNIQUE value | accepted | `IntegrityError` |
| CHECK violation (`age = -5`) | accepted | `IntegrityError` |
| Orphan FK row | accepted | accepted |
| Unique index | dropped | copied |

The sandbox DDL for the Postgres case contains no `UNIQUE`, `CHECK`,
`FOREIGN KEY` or `DEFAULT` at all.

Three further observations:

- **The sandbox is also stricter, not only looser.** With `server_default`
  dropped, `plan` stays `NOT NULL` with no default, so an INSERT that omits it —
  legal in production — fails in the sandbox with `IntegrityError`. Agents get
  false alarms as well as false clearances.
- **Foreign keys are never enforced on either path.** SQLite ships with
  `PRAGMA foreign_keys = OFF` and nothing in the library turns it on, so even the
  faithful SQLite clone accepts orphan rows.
- **Which path you get hinges on an incidental detail.** `_detect_dialect()`
  sniffs the `__module__` of column types. A Postgres table built entirely from
  generic SQLAlchemy types is classified `sqlite` and keeps its constraints;
  adding one `JSONB` column to the same table silently drops all of them.
  `test_observed_fidelity_flips_on_one_column_type` asserts both outcomes.

**Sub-finding (SQLite path, separate failure mode).** The reflected CHECK
expression is re-emitted verbatim. SQLAlchemy's SQLite CHECK reflection is
regex-based and swallows the table's closing paren when it is on the same line,
producing the unbalanced text `age >= 0)`. `ShadowDB.__enter__` then raises
`OperationalError: near ")": syntax error`. Whether a production table can be
sandboxed at all depends on where a newline sits in its `CREATE TABLE`.
(`test_observed_sqlite_check_reflection_can_crash_sandbox_creation`)

**Practical impact.** For the library's headline use case — a Postgres
production database — the sandbox enforces nothing but `NOT NULL` and the
primary key. An agent can write a duplicate email, a negative balance, or an
orphaned foreign key; `diff()` renders `[SAFE] AI CHANGES VERIFIED`; the commit
then fails with a raw `IntegrityError` from the driver. Worse, any invariant
carried by a CHECK constraint that Pydantic does not also encode is unguarded
right up to the write. Users on SQLite are not exposed to the constraint loss,
but may hit the reflection crash instead.

---

## 2. Lost update — CONFIRMED

**Code path.** [sandbox.py:86](../safeagentdb/sandbox.py#L86) takes
`_original_snapshot` at clone time; [`commit_to_production()`](../safeagentdb/sandbox.py#L130-L158)
diffs against that snapshot and hands the result to
[`sync.apply_changeset()`](../safeagentdb/sync.py#L86-L91), which issues
`UPDATE … SET <every column of diff.new> WHERE pk AND tenant`. Nothing compares
the production row against `diff.old`.

**What the test does.** A `tasks` row is cloned into the sandbox; the agent sets
`status = 'done'`; a **second engine on the same file** (a separate connection,
standing in for another process) then renames `title`; the sandbox commits.

**Observed behaviour.** The diff reports `changed_columns() == ['status']` and
carries `new['title'] == 'Ship v2'` — the clone-time value. `UPDATE` writes all
four columns, so after the commit `title == 'Ship v2'`: the human's rename is
gone. No warning, no error, `commit_to_production()` returns `1`.

**Related, same code path.** `apply_changeset` does `affected += 1` per diff
([sync.py:100](../safeagentdb/sync.py#L100)) without consulting `rowcount`. If
the concurrent process *deletes* the row instead, the `UPDATE` matches nothing,
yet `commit_to_production()` still returns `1`. The return value cannot be used
to detect that a write was silently dropped.
(`test_observed_commit_count_overstates_rows_written`)

**Practical impact.** The sandbox is advertised as the safe way to let an agent
touch a live database, so it will be pointed at databases with live human
traffic. Any write landing between clone and commit is reverted — including
writes to columns the agent never touched, since the UPDATE is a whole-row
overwrite. A long-running agent session widens the window arbitrarily. The
failure is silent: nothing in `diff()`, the return value, or the logs indicates
that data was lost.

---

## 3. Validator inconsistency — CONFIRMED

**Code path.** [diff.py:69-71](../safeagentdb/diff.py#L69-L71) —
`RowDiff.validate()` returns `(True, "No validator")` when
`get_validator()` is `None`. [models.py:59-64](../safeagentdb/models.py#L59-L64) —
`validate_row()` raises `KeyError` for exactly the same condition, and
[sync.py:73](../safeagentdb/sync.py#L73) calls it unguarded.

**What the test does.** With an empty registry, an agent updates a `tasks` row;
the test inspects the changeset, then commits.

**Observed behaviour.**

- `diff.validate()` → `(True, 'No validator')`
- `changeset.is_valid` → `True`
- `changeset._render_plain()` → `[SAFE] AI CHANGES VERIFIED -- SAFE TO COMMIT`
- `commit_to_production()` → `KeyError: "No SafeModel registered for table
  'tasks'. …"`

The `KeyError` is neither `SyncError` nor `pydantic.ValidationError`, the two
exception types `commit_to_production()` documents. Production is left
untouched, because `engine.begin()` rolls back.

**Practical impact.** The dashboard is the review surface — a human or an agent
reads `[SAFE]` and approves. The commit then dies on an exception type nobody
catches, so `except (SyncError, ValidationError)` handlers miss it and the
process crashes. In a multi-table changeset, one unregistered table aborts the
whole transaction after every other row has already validated. The failure is
also non-obvious: a table quietly dropped out of the registry (a typo in
`__table_name__`, a forgotten import) reads as *more* safe in the diff, not less.

---

## 4. Tables without a primary key — CONFIRMED (found during review)

**Code path.** [sandbox.py:191-195](../safeagentdb/sandbox.py#L191-L195)
`_pk_columns()` returns `[]` for a table with no primary key.
[diff.py:392-393](../safeagentdb/diff.py#L392-L393) `_pk_key(row, [])` then
returns `()` for **every** row, so
[`compute_diff()`](../safeagentdb/diff.py#L345-L389) collapses the table into a
single dict entry (last row wins). [sync.py:86-98](../safeagentdb/sync.py#L86-L98)
builds `update(table)` / `delete(table)` by iterating `diff.pk`, which is `{}`,
leaving the tenant column as the **only** predicate.

**What the test does.** An append-only `events` table with no PK holds three
tenant-42 rows plus one tenant-99 row. Three scenarios: edit the last cloned
row, delete a row, edit a non-last row.

**Observed behaviour.**

- `_pk_columns()` → `{'events': []}`; all three rows key to `()`.
- Editing the last row: diff reports exactly one `UPDATE` with `pk == {}`,
  `is_valid` is `True`, commit returns `1` — and production goes from
  `(login,a) (click,b) (logout,c)` to **three identical `(logout, EDITED)`
  rows**. The emitted statement is `UPDATE events SET … WHERE user_id = 42`.
- Deleting a row: reported as an `UPDATE`, not a `DELETE`; production ends up as
  three copies of `(click, b)`.
- Editing a non-last row: `changeset.is_empty` is `True`, commit returns `0`, the
  change never reaches production.

Tenant isolation holds throughout — the tenant-99 row is untouched. The damage
is confined to the tenant, and total within it.

**Practical impact.** The most severe of the four. Junction tables, audit logs,
event streams and append-only tables commonly have no primary key, and nothing
in the library warns about this: `ShadowDB` accepts the table, clones it, shows a
clean one-row diff, passes Pydantic validation, and reports success. One agent
edit silently overwrites every row that tenant owns in that table, or is silently
discarded. A reviewer reading the diff dashboard sees a single-row change in both
cases. Rejecting PK-less tables at `__enter__` would be strictly safer than the
current behaviour.

---

## Summary

| # | Claim | Verdict |
|---|---|---|
| 1 | Sandbox drops UNIQUE / CHECK / FK / defaults | **CONFIRMED** for Postgres & MySQL; **NOT CONFIRMED** for SQLite production (different branch). FKs unenforced on both paths. |
| 2 | Concurrent production writes silently overwritten | **CONFIRMED** (+ `affected` count overstates rows written) |
| 3 | `RowDiff.validate()` and `sync.validate_row()` disagree | **CONFIRMED** |
| 4 | PK-less tables: mass overwrite or silent drop | **CONFIRMED** |

Two smaller observations, not filed above:

- `tests/test_core.py` is a script, not a pytest module. It executes at
  collection time, writes `release_test.db` into the current working directory,
  registers three validators into the process-global `_model_registry` and never
  clears them, and would call `sys.exit(1)` on failure.
- `safeagentdb/engine.py` imports a dozen dialect type names
  ([lines 36-76](../safeagentdb/engine.py#L36-L76)) that are never referenced —
  `_SQLITE_TYPE_MAP` is keyed by string. `_sqlite_safe_type()` matches on class
  name only, so generic `sqlalchemy.ARRAY` and `sqlalchemy.JSON` also hit the
  Postgres entries, and MySQL `TINYINT` is mapped to `String(255)`.
