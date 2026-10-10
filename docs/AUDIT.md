# SafeAgentDB — Weakness Audit

Date: 2026-09-20 · Version audited: 0.1.2 · Branch: `audit`

> **Status: findings 1-4 fixed in 0.2.0; findings 5-10, from a second
> independent review of 0.2.0, fixed in 0.3.0.** See
> [CHANGELOG.md](../CHANGELOG.md) for what changed and which changes are
> breaking. The tests referenced below were rewritten as regression guards and
> now assert the fixed behaviour; this document is kept as the record of what
> was wrong and why.

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

## Summary -- first review (0.1.2)

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


---

# Second review -- 0.2.0 at `9dea013`

Date: 2026-10-10 · Version audited: 0.2.0 · Branch: `hardening-2`

An independent review of the released 0.2.0 found six further issues. All six
were reproduced against the published package before anything was changed; each
has a failing-first test in
[tests/test_review_findings.py](../tests/test_review_findings.py), and findings
6, 7 and 10 also have PostgreSQL versions in
[tests/test_server_backed.py](../tests/test_server_backed.py).

**All six CONFIRMED and fixed in 0.3.0.** 203 local tests pass, plus 19
server-backed tests against PostgreSQL in CI.

---

## 5. Duplicate custom row key overwrote a production row — CONFIRMED

**Code path.** [diff.py `compute_diff()`](../safeagentdb/diff.py) indexed both
snapshots with a dict comprehension, `{_pk_key(r, pks): r for r in rows}`. Two
rows sharing a key collapsed to one entry, last writer winning.
[sandbox.py `_assert_row_keys_unique()`](../safeagentdb/sandbox.py) checked
uniqueness only at `__enter__`, against the cloned rows.

**What the test does.** A PK-less `events` table with
`row_key={"events": ["kind"]}`; production holds `('a', 42, 'original')`. The
sandbox inserts a second row with `kind='a'`.

**Observed behaviour.** `diff()` reported one UPDATE and `is_valid` was `True`.
The commit ran `UPDATE events SET ... WHERE kind='a' AND tenant_id=42`, so two
rows went into the sandbox, one row came out of production, and the row that
survived held the *new* payload. The original data was destroyed and the second
row was never written.

**Practical impact.** The most severe of the six. A custom `row_key` is
recommended in the README for PK-less tables, and nothing stops an agent
inserting a row that collides on it — audit logs and event streams, the exact
tables that need `row_key`, are also the ones where duplicate natural keys are
most likely. Silent, and it destroys data rather than failing.

**Fixed by** three guards, one at each point the collision can appear:
`diff()` re-checks uniqueness on the *current* sandbox state and returns a
changeset carrying a blocking error; `compute_diff()` indexes through
`_keyed_rows()`, which raises `SchemaError` rather than picking a winner; and
`apply_changeset()` refuses an INSERT whose key production already holds with
`DuplicateRowKeyError`.

---

## 6. ON DELETE CASCADE crossed the tenant boundary — CONFIRMED

**Code path.** [sync.py `apply_changeset()`](../safeagentdb/sync.py) put the
tenant predicate in the WHERE clause of every statement it issued. A referential
action makes the database act on *further* rows by itself, and no WHERE clause
of ours constrains those.

**What the test does.** `parent(id, tenant_id)` with row 1 owned by tenant 42;
`child(tenant_id=99, parent_id REFERENCES parent(id) ON DELETE CASCADE)`. Tenant
42's sandbox deletes the parent.

**Observed behaviour.** The delete succeeded and took tenant 99's child row with
it. `unsupported_constraints` was empty and nothing warned, at open or at
commit.

**Practical impact.** A tenant-isolation breach in the one direction the library
claims to prevent, through a schema feature that is entirely ordinary. The
sandbox could not have shown it either: `child` was never cloned.

**Fixed by** inspecting the foreign keys of *every* production table at
`__enter__` — not only the cloned ones, since the dangerous child is the one
nobody asked to sandbox — for propagating actions (`CASCADE`, `SET NULL`,
`SET DEFAULT`, on delete or update) into a cloned table. Each is recorded in
`unsupported_constraints`, rendered in the diff, and exposed as
`ShadowDB.cascade_references`. At commit, before a DELETE or an UPDATE touching
a referenced column, the referencing rows are counted per tenant inside the
transaction; rows belonging to another tenant, or in a child table with no
tenant column at all, raise `CascadeError` and roll the changeset back. A
cascade confined to the acting tenant still applies.

**Worth knowing:** SQLAlchemy's SQLite inspector returns an empty `options`
dict for every foreign key, so the referential action is invisible there.
`PRAGMA foreign_key_list` reports it; PostgreSQL and MySQL use the inspector.

---

## 7. A populated JSON column crashed the sandbox open — CONFIRMED

**Code path.** [engine.py `load_rows()`](../safeagentdb/engine.py) inserted
production values straight into the sandbox. `_sqlite_safe_type()` remapped the
*column type* to `Text`, but nothing converted the *values*.

**What the test does.** A production column `data JSON` holding `{"a": 1}`.

**Observed behaviour.** `__enter__` raised
`sqlite3.ProgrammingError: type 'dict' is not supported` — a raw driver
exception, outside the documented hierarchy. SQLAlchemy's JSON result processor
returns a `dict`; SQLite can bind only `None`, `int`, `float`, `str` and
`bytes`.

**Practical impact.** The library could not be used at all against any
PostgreSQL schema with a populated `JSON`/`JSONB`/`ARRAY`/`HSTORE` column — a
common shape — and the failure was a bare driver error rather than anything a
caller could act on.

**Fixed by** recording a conversion for every remapped column: `json.dumps` for
JSON, JSONB, ARRAY and HSTORE, `str` for UUID, INET and the rest, with values
SQLite can already bind passed through untouched. The value is converted back
before it is written at commit. `RowDiff.logical_row()` produces the production
representation and is used for change detection, `RowDiff.validate()` and sync's
Pydantic gate alike — the `SafeModel` describes production, not the sandbox — so
a value that differs only in serialisation is not reported as changed. Remapped
columns are excluded from the compare-and-swap guard, whose two sides hold
different representations. Any `SQLAlchemyError` during `__enter__` is now
wrapped in `SchemaError`.

---

## 8. diff() reported SAFE for a change the commit rejected — CONFIRMED

**Code path.** The tenant guard lived only in
[sync.py `apply_changeset()`](../safeagentdb/sync.py), inside the commit
transaction. [diff.py `ChangeSet.is_valid`](../safeagentdb/diff.py) consulted
only per-row Pydantic validation.

**What the test does.** `UPDATE tasks SET tenant_id=99 WHERE id=1`.

**Observed behaviour.** `diff().is_valid` was `True` and the banner read
`[SAFE] AI CHANGES VERIFIED -- SAFE TO COMMIT`. The commit then raised
`SyncError` for a tenant breach.

**Practical impact.** The diff is the review surface — the thing a human or an
approving agent reads before saying yes. Having it disagree with the gate is the
worst place for a disagreement: it trains reviewers to trust a green banner that
does not mean what it says, and in a multi-table changeset the rejection arrives
after everything else has already validated.

**Fixed by** moving the tenant rule into one function, `diff.tenant_breach()`,
called by `apply_changeset()` before it writes and by `ShadowDB.diff()` so the
dashboard reaches the same verdict. The reference-table and row-key checks join
it in a pre-flight pass whose results land in `ChangeSet.blocking_errors`, so
`is_valid` and both renderers reflect them. Drift and cross-tenant uniqueness
stay commit-only because they genuinely need production objects — and a clean
banner now says so instead of promising success.

---

## 9. The reviewed changeset was not the committed changeset — CONFIRMED

**Code path.** [sandbox.py `commit_to_production()`](../safeagentdb/sandbox.py)
called `self.diff()` itself and committed whatever that returned. The
`ChangeSet` a caller had already inspected played no part.

**What the test does.** `diff()` is called and shows one UPDATE; a DELETE is
then executed in the sandbox; `commit_to_production()` is called.

**Observed behaviour.** Two changes were written. The changeset that was
reviewed and the changeset that landed were different objects, and nothing
compared them.

**Practical impact.** It makes the approval step advisory rather than binding.
Any pattern of the form *render the diff, ask a human, commit* is unsound: a
second agent turn, a retry, or a background task touching the session between
the review and the commit silently widens what gets written.

**Fixed by** `ChangeSet.fingerprint`, a content-addressed, order-independent
sha256 of exactly what the changeset would write, and
`commit_to_production(changeset=reviewed)`, which recomputes the diff and
refuses with `ChangesetMismatchError` when the fingerprints differ. Calling with
no argument keeps the 0.2.x behaviour; the README now uses the pinned form in
its approval examples.

---

## 10. Statement order ignored foreign-key dependencies — CONFIRMED

**Code path.** [sync.py `_statement_order()`](../safeagentdb/sync.py) sorted by
`(table name, row key, operation)`. Table name was alphabetical, with no regard
for which table referenced which.

**What the test does.** `b_parent` and `a_child`, the child referencing the
parent. Both rows are inserted in the sandbox.

**Observed behaviour.** `a_child` sorted first, so the child was inserted before
its parent and production rejected it with a foreign-key violation. The
changeset was correct; only the order was wrong.

**Practical impact.** Any changeset creating a parent and a child together fails
whenever the child's table name sorts first — roughly half of all such schemas,
decided by nothing more than naming. The error it produced then compounded the
problem: `IntegrityViolationError` told the caller the rejection happened
"because it holds only this tenant's rows", which here was simply untrue.

**Fixed by** ordering tables topologically on the foreign-key graph (Kahn's
algorithm, ties broken by name). DELETEs run first, children before parents;
then INSERTs and UPDATEs, parents before children, so a row deleted and
re-inserted under the same key still goes in the right order. Within a table the
existing row-key order stands, and the whole order remains a total deterministic
function of the changeset, which is what the deadlock argument rests on. A
dependency cycle raises `SchemaError` naming the tables rather than being
attempted. `IntegrityViolationError` now reports what production said and that
the sandbox could not evaluate the rule, and claims nothing further.

---

## Summary -- second review (0.2.0)

| # | Finding | Verdict |
|---|---------|---------|
| 5 | Duplicate custom row key overwrote a production row | **CONFIRMED** |
| 6 | ON DELETE CASCADE crossed the tenant boundary | **CONFIRMED** |
| 7 | A populated JSON column crashed the sandbox open | **CONFIRMED** |
| 8 | diff() reported SAFE for a change the commit rejected | **CONFIRMED** |
| 9 | The reviewed changeset was not the committed changeset | **CONFIRMED** |
| 10 | Statement order ignored foreign-key dependencies | **CONFIRMED** |

### What this round says about the first round

Findings 6, 7 and 10 all share a shape: a behaviour that is invisible on SQLite
and only appears against a real server, or only appears in a schema feature the
first round's tests never used. The first audit was written entirely against
SQLite, and 0.2.0's PostgreSQL job existed but had not yet run when 0.2.0 was
cut. Three of these six would have been caught earlier by a server-backed test
of ordinary schema features — cascading keys, JSON columns, a parent and a child
in one changeset — rather than by more tests of the paths already covered.
