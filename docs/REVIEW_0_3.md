# SafeAgentDB — Third Review (pre-0.3.0)

Date: 2026-10-13 · Branch: `hardening-2` at `f5043f5` · Python 3.11.9,
SQLAlchemy 2.0.35, pydantic 2.13.4, Windows.

A pre-release review of 0.3.0 that went looking for the next layer below the ten
findings in [AUDIT.md](AUDIT.md). It covered diff/commit consistency and row keys,
type round-trips, error handling and API usage, and an audit of every claim
in the README. No library code was changed.

**Every finding below was reproduced** with a script against a local SQLite file
database standing in for production. The scripts are in
[review-repros/](review-repros/), one folder per review piece. Anything that
could not be reproduced is listed separately under
[Suspected, not reproduced](#suspected-not-reproduced).

Not yet covered: an adversarial probe of tenant scoping (every path by which
the agent might reach another tenant's rows). The claims audit below found no
reproduced finding that crosses the tenant boundary, but that is not the same as
having looked for one.

## Status after the fixes

Added after the review, on `hardening-2`. Each finding's reproduction became a
test in `tests/test_review_0_3.py` that failed first, then was fixed; the
server-dependent findings also have PostgreSQL tests in
`tests/test_server_backed.py`. The reproduction scripts below are kept exactly
as they were run, so a few now stop on the new, intended errors (for example
`p3a/a1_api_usage.py` step 6, `diff()` after a commit, now raises `UsageError`).

| ID | Status | Resolution |
|----|--------|------------|
| P1-1 | Fixed | Key changes are logged by `TEMP` triggers in the sandbox and refused in `diff()` and at commit. Independently, a referential action on rows the diff does not show raises `CascadeError` unless `allow_cascades=True`. |
| P1-2 | Fixed | Unit-of-work order: INSERTs parents first, UPDATEs, DELETEs children first; rows ordered by their references. |
| P1-3 | Fixed | Change detection and `changed_columns()` use a type-tagged canonical form. |
| P1-4 | Fixed (SQLite) | Referential actions read from `PRAGMA foreign_key_list` and carried into the sandbox. |
| P1-5 | Fixed | Generated keys tracked, not detected by value. |
| P1-6 | Fixed | Type-tagged fingerprint; bind-time errors wrapped. |
| P2-1 | Fixed (SQLite, by exclusion) | `DATETIME`, `DATE`, `TIME`, `BOOLEAN` left out of the guard on SQLite production; documented as a known limit. A conflict with no drifted column no longer blames another writer. |
| P2-2 | Fixed | JSON values held as their JSON encoding, scalars included. |
| P2-3 | Fixed | Datetimes held as ISO 8601 text with their offset; SQLite production read and written as text; `timestamptz` tested on PostgreSQL. |
| A1 | Fixed | `SchemaError`, driver error as `__cause__`. |
| A2 | Fixed | Every database error during a commit is a `SyncError`. |
| A3 | Fixed | See P1-6. |
| A4 | Fixed | Agent DDL raises `SchemaError` from `diff()` and the commit. |
| A5 | Fixed | `AgentSQLError`; no bind parsing without `params`; `Decimal` supported. |
| A6 | Fixed | `UsageError` (also a `RuntimeError`); commit-time validation is `RowValidationError`. |
| B1 | Fixed | `tenant_id` normalised to the tenant column's type at `__enter__`. |
| B2 | Fixed | See P1-2; a real row cycle is refused in `diff()`. |
| B3–B6, B8 | Fixed | Aliases, resolved with their targets. |
| B7 | Fixed | Every failing field with pydantic's reason and the value. |
| C1 | Fixed | `diff()` after a commit raises `UsageError`. |
| C2 | Fixed | Entering an open sandbox raises `UsageError` and leaves it intact. |
| C3 | Fixed, documented | Re-entry allowed until a commit, refused after. |
| Suspected: MySQL cascade quoting | Fixed | Cascade check built from SQLAlchemy Core. |
| Suspected: MySQL `TINYINT` | Fixed | Mapped to an integer. |
| Suspected: custom `row_key` with a generated PK | Fixed | Refused with a message naming the re-insert. |
| Suspected: P1-1/P1-2 and `timestamptz` on PostgreSQL | Tested | Covered by the server-backed suite. |

## Running the reproductions

```bash
python docs/review-repros/p1/f2b_pk_change_uncloned_child.py
```

Each script is standalone. `_h.py` (identical in every folder) puts the
repository root on `sys.path`, so the scripts exercise the working-tree library,
and creates a fresh SQLite file database per scenario with
`PRAGMA foreign_keys=ON`, as a production SQLite deployment would set it. Every
script exits 0 except `p1/f2a_pk_change_cloned_child.py`, which ends in a raw
`IntegrityError` from the sandbox; that error is the evidence for P1-4.

The folder has its own `ruff.toml` that relaxes layout-only lint rules, so the
scripts stay as they were run.

---

## Index

20 distinct findings: **3 high, 9 medium, 8 low**. IDs that only cross-reference
another finding are marked *alias*.

| ID | Severity | Summary |
|----|----------|---------|
| [P1-1](#p1-1) | High | A primary-key change is committed as DELETE + INSERT, so production runs `ON DELETE` actions instead of `ON UPDATE` |
| [P1-2](#p1-2) | High | All DELETEs run before any UPDATE; re-pointing a child then deleting its old parent fails, or loses the child under `ignore` |
| [P1-3](#p1-3) | Medium | A JSON change that Python considers equal (`1` → `true`) is shown as an UPDATE but never written |
| [P1-4](#p1-4) | Medium | With SQLite production, the sandbox drops every `ON DELETE`/`ON UPDATE` action |
| [P1-5](#p1-5) | Medium | Any integer ≥ 2^52 in any column is treated as a placeholder key and blocks the row |
| [P1-6](#p1-6) | Low | The review fingerprint collides across value types; the commit then fails outside the hierarchy |
| [P2-1](#p2-1) | High | False `ConflictError` on DELETE/UPDATE when a datetime or boolean is stored in another text format |
| [P2-2](#p2-2) | Medium | JSON scalars change type in the sandbox (`"123"` → `123`, `true` → `1`, `"null"` → `null`) |
| [P2-3](#p2-3) | Medium | A datetime's UTC offset is dropped in the sandbox and written back without it |
| [A1](#a1) | Medium | Database errors raised while `__enter__` reads the production schema are not wrapped |
| [A2](#a2) | Medium | Whether a commit-time database error is wrapped depends on the changeset's shape |
| A3 | — | *Alias of [P1-6](#p1-6)* (bind-time error at commit escapes) |
| [A4](#a4) | Low | `diff()`/`commit_to_production()` fail with raw errors after the agent changes the sandbox schema |
| [A5](#a5) | Low | `execute()`/`query()` pass driver errors through unwrapped, including on valid input |
| [A6](#a6) | Low | Misuse errors are plain `RuntimeError`/`ValueError`/`AttributeError` |
| [B1](#b1) | Medium | A string `tenant_id` makes every write a "Tenant breach" |
| [B2](#b2) | Medium | Rows of a self-referencing table are written in key order; production rejects them with a message that names the wrong cause |
| B3 | — | *Alias of [P1-2](#p1-2)* (wrong-cause `IntegrityViolationError` / `ConflictError`) |
| B4 | — | *Alias of [P1-1](#p1-1)* (error names a DELETE the agent never issued) |
| B5 | — | *Alias of [P1-5](#p1-5)* (`GeneratedValueError` for an ordinary large integer) |
| B6 | — | *Alias of [P2-1](#p2-1)* (`ConflictError` with no drift) |
| [B7](#b7) | Low | The per-row validation message names only the field |
| B8 | — | *Alias of [C1](#c1)* (SAFE banner after commit) |
| [C1](#c1) | Low | `diff()` after a successful commit shows the committed changes as SAFE TO COMMIT |
| [C2](#c2) | Low | `__enter__` on an open sandbox silently discards the agent's work and leaks the old engine |
| [C3](#c3) | Low | Re-entering a committed sandbox opens one that accepts edits but can never commit |

---

## Piece 1 — diff/commit consistency and row keys

<a id="p1-1"></a>
### P1-1 · High · A primary-key change is committed as DELETE + INSERT

**Code path.** `compute_diff()` keys rows on the primary key, so
`UPDATE parent SET id = 5 WHERE id = 1` becomes a DELETE of key 1 and an INSERT
of key 5 ([diff.py:688-712](../safeagentdb/diff.py#L688-L712)). At commit,
`_refuse_cross_tenant_cascade()` allows a cascade confined to the acting tenant
([sync.py:319](../safeagentdb/sync.py#L319)), and production runs a real DELETE,
firing the child's `ON DELETE` action. The `ON UPDATE` action the agent's
statement would have triggered never runs.

**Repro.** [p1/f2b_pk_change_uncloned_child.py](review-repros/p1/f2b_pk_change_uncloned_child.py):
child FK `ON UPDATE CASCADE ON DELETE CASCADE`, child not cloned, two child rows
in the same tenant.

```
diffs: [('parent', 'DELETE', {'id': 1}), ('parent', 'INSERT', {'id': 5})]
is_valid: True | [SAFE] AI CHANGES VERIFIED -- SAFE TO COMMIT
commit wrote 2
prod parent: [(5, 42)]
prod child : []
```

The same UPDATE run directly in production re-points both children to 5.
[p1/f2c_pk_change_restrict.py](review-repros/p1/f2c_pk_change_restrict.py)
(`ON UPDATE CASCADE ON DELETE RESTRICT`) shows the other outcome: the statement
is legal in production (control step), the diff is SAFE, and the commit fails
with `IntegrityViolationError` for "the DELETE on 'parent'", which the agent
never issued.

**Impact.** A routine key change silently deletes the tenant's child rows behind
a SAFE banner, and the returned count does not include them. Where the delete
action is `RESTRICT`, a legal change cannot be committed at all.

<a id="p1-2"></a>
### P1-2 · High · All DELETEs run before any UPDATE

**Code path.** `_PHASE = {DELETE: 0, INSERT: 1, UPDATE: 2}` in
`_order_diffs()` ([sync.py:544-573](../safeagentdb/sync.py#L544-L573)). The
topological fix for finding 10 orders tables within a phase, but nothing orders
an UPDATE ahead of a DELETE that depends on it.

**Repro.** [p1/f1a_reparent_then_delete.py](review-repros/p1/f1a_reparent_then_delete.py):
plain FK. The agent moves child 10 from parent 1 to parent 2, then deletes
parent 1. The sandbox accepts both statements.

```
summary: {'INSERT': 0, 'UPDATE': 1, 'DELETE': 1} is_valid: True
banner: [SAFE] AI CHANGES VERIFIED -- SAFE TO COMMIT
commit raised IntegrityViolationError -> Production rejected the DELETE on 'parent' for row {'id': 1}:
  FOREIGN KEY constraint failed. ... the rule that rejected it is one the tenant-scoped clone could not evaluate.
```

[p1/f1b_reparent_cascade_ignore.py](review-repros/p1/f1b_reparent_cascade_ignore.py),
with `ON DELETE CASCADE`:

```
[abort]  commit raised ConflictError -> Row {'id': 10} in 'child' was deleted in production after the sandbox was created
[ignore] commit wrote 1, ConflictWarning, skipped: [('child', {'id': 10})]
[ignore] prod child : []
```

**Impact.** Re-parenting before a delete is a common pattern and never commits.
The `abort` error blames a concurrent writer that does not exist, and the
`IntegrityViolationError` text says the sandbox could not evaluate a rule it
did evaluate. Under `on_conflict="ignore"`, a row the diff showed as *updated* is
destroyed.

<a id="p1-3"></a>
### P1-3 · Medium · A JSON change Python considers equal is never written

**Code path.** `compute_diff()` detects a changed row by comparing raw sandbox
text ([diff.py:715](../safeagentdb/diff.py#L715)); `changed_columns()` compares
parsed JSON with `==`, where `1 == True == 1.0`
([diff.py:99-101](../safeagentdb/diff.py#L99-L101)). With no changed columns,
sync reaches `continue` and writes nothing
([sync.py:219-220](../safeagentdb/sync.py#L219-L220)).

**Repro.** [p1/f4_json_logical_equality.py](review-repros/p1/f4_json_logical_equality.py)

```
row {'id': 1}: UPDATE, changed_columns=[]     {"enabled": 1} -> {"enabled": true}
row {'id': 2}: UPDATE, changed_columns=[]     {"a": 1} rewritten as {"a":1}
summary: {'INSERT': 0, 'UPDATE': 2, 'DELETE': 0} is_valid: True   [SAFE], ~2 update, empty change table
commit wrote 0
prod: [(1, '{"enabled": 1}'), (2, '{"a": 1}')]
```

**Impact.** A real type change inside a JSON value is dropped without an error. A
formatting-only rewrite shows up as an update with nothing in it.

<a id="p1-4"></a>
### P1-4 · Medium · With SQLite production, the sandbox drops FK referential actions

**Code path.** The sandbox is built with `table.to_metadata()` from reflection
([engine.py:669](../safeagentdb/engine.py#L669)). SQLAlchemy's SQLite inspector
does not report `ON DELETE`/`ON UPDATE`. `find_cascade_references()` works around
this with `PRAGMA foreign_key_list`
([engine.py:563-569](../safeagentdb/engine.py#L563-L569)), but nothing applies the
actions to the sandbox's own foreign keys, and the loss is not listed in
`unsupported_constraints`.

**Repro.** [p1/f3_sandbox_drops_fk_actions.py](review-repros/p1/f3_sandbox_drops_fk_actions.py)

```
production DDL : ... REFERENCES parent(id) ON DELETE CASCADE ON UPDATE CASCADE
sandbox DDL    : ... FOREIGN KEY(parent_id) REFERENCES parent (id)
cascade_references: [('child', 'CASCADE', 'CASCADE'), ('note', 'SET NULL', None)]
DELETE FROM parent WHERE id = 1 -> sandbox raised IntegrityError FOREIGN KEY constraint failed
control in prod: child [] note [(20, 42, None)]
```

[p1/f2a_pk_change_cloned_child.py](review-repros/p1/f2a_pk_change_cloned_child.py)
shows the same thing for a key change.

**Impact.** SQLite production only. Deletes and key changes that production
accepts are refused in the sandbox, and a cascade cannot be previewed even when
the child table is cloned.

<a id="p1-5"></a>
### P1-5 · Medium · Any integer ≥ 2^52 is treated as a placeholder key

**Code path.** `provisional_reference_columns()`
([diff.py:152-167](../safeagentdb/diff.py#L152-L167)) decides by value alone
(`_is_provisional`, [diff.py:614-620](../safeagentdb/diff.py#L614-L620)). It runs
on UPDATEs as well as INSERTs, over every column, whether or not the table has a
provisional key.

**Repro.** [p1/f5_large_int_false_provisional.py](review-repros/p1/f5_large_int_false_provisional.py):
a `BIGINT external_id` holding a snowflake-style ID; the agent changes only `name`.

```
provisional_key_columns: {} generated: {}
is_valid: False | [BLOCKED] SAFETY ALERT -- INVALID DATA DETECTED
row message: column(s) external_id in 'accounts' reference a key that production has not assigned yet ...
commit raised GeneratedValueError columns= ['external_id']
```

**Impact.** A row holding a large 64-bit identifier (snowflake, Twitter or
Discord IDs, hashes) can never be inserted or updated, and the error names a
cause that does not apply.

<a id="p1-6"></a>
### P1-6 · Low · The fingerprint collides across types; the commit then escapes the hierarchy

**Code path.** `ChangeSet.fingerprint` serialises values with
`json.dumps(..., default=str)` ([diff.py:261-272](../safeagentdb/diff.py#L261-L272)).
`_execute()` catches only `IntegrityError` and `DBAPIError`
([sync.py:656-676](../safeagentdb/sync.py#L656-L676)), so a bind-time error
arrives as a raw `StatementError`. (This is also A3.)

**Repro.** [p1/f6_fingerprint_type_collision.py](review-repros/p1/f6_fingerprint_type_collision.py):
a BLOB column reviewed holding `b'x'`, then set to the text `"b'x'"`.

```
after change: "b'x'"; same fingerprint: True
raised sqlalchemy.exc.StatementError (SafeAgentDBError: False): (builtins.TypeError) memoryview: a bytes-like object is required, not 'str'
prod: [(1, None, 'null')]
```

**Impact.** Limited. The pinned-changeset check accepts content that was not
reviewed, but in the only colliding case found the write then fails, so nothing
lands. The failure gets past `except SafeAgentDBError`.

---

## Piece 2 — type round-trips

<a id="p2-1"></a>
### P2-1 · High · False `ConflictError` when a value is stored in another text format

**Code path.** `_guard_columns()` treats `DateTime` and `Boolean` as safe to
compare ([sync.py:594-628](../safeagentdb/sync.py#L594-L628)). `_matches()`
binds the clone-time Python value through the production column type, so the
WHERE clause compares `'2024-01-01 10:00:00.000000'` or `1` with the stored text,
and SQLite compares text literally. `_diagnose_conflict()` re-reads the row,
finds the Python values equal, and reports a generic "changed in production"
with no column named ([sync.py:728-736](../safeagentdb/sync.py#L728-L736)).

**Repro.** [p2/t1_untouched_and_delete.py](review-repros/p2/t1_untouched_and_delete.py)
(DELETE) and [p2/t2_changed_values.py](review-repros/p2/t2_changed_values.py)
(UPDATE of the column). Nothing else writes to production between clone and commit.

```
row 11 ('2024-01-01 10:00:00'):   ConflictError: ... changed in production after the sandbox was created, so the DELETE matched nothing.
row 12 ('2024-01-01T10:00:00'):   ConflictError: ... DELETE matched nothing.
row 14 (flag 'true'):             ConflictError: ... DELETE matched nothing.
row 11 ts <- '2025-03-01 08:30:00': ConflictError: ... the UPDATE matched nothing.
row 14 flag <- False:               ConflictError: ... the UPDATE matched nothing.
```

**Impact.** `'YYYY-MM-DD HH:MM:SS'` is exactly what SQLite's own
`DEFAULT CURRENT_TIMESTAMP` writes, so on SQLite production any row with such a
`created_at` can never be deleted, and its timestamp cannot be changed. The error
blames a concurrent writer that does not exist. Under `on_conflict="ignore"`
the rows are skipped with a misleading warning.

<a id="p2-2"></a>
### P2-2 · Medium · JSON scalars change type in the sandbox

**Code path.** `to_sandbox_value()` leaves any value SQLite can bind as it is
([engine.py:103-104](../safeagentdb/engine.py#L103-L104)): the JSON string `"123"`
arrives as the plain text `123`, and JSON `true` as `1` (`bool` is an `int`).
`to_production_value()` runs `json.loads` on every string
([engine.py:114-119](../safeagentdb/engine.py#L114-L119)), turning `'123'` into
`123` and `'null'` into `None`. The same helper feeds `RowDiff.logical_row()`,
so validation and the commit both see the wrong type.

**Repro.** [p2/t5_copy_scalars_and_tz.py](review-repros/p2/t5_copy_scalars_and_tz.py):
the agent copies rows with `INSERT ... SELECT`, changing no value.

```
is_valid True | [SAFE] AI CHANGES VERIFIED -- SAFE TO COMMIT
  INSERT {'id': 11}: code shown '123' -> writes 123
  INSERT {'id': 12}: code shown 'null' -> writes None
  prod (1,  '"123"',  'text')       source
  prod (11, 123,      'integer')    copy: string became a number
  prod (2,  '"null"', 'text')
  prod (12, 'null',   'text')       copy: the string "null" became JSON null
```

[p2/t3_json_scalars.py](review-repros/p2/t3_json_scalars.py): with a `SafeModel`
typing the column `Optional[bool]`, a row holding JSON `true` is BLOCKED when the
agent edits only another column (message: `enabled`), and the commit raises
`pydantic.ValidationError`.

**Impact.** Any INSERT, or any UPDATE of such a column, writes a different JSON
type than production held, behind a SAFE banner. A typed validator also blocks
legitimate edits to unrelated columns of the same row.

<a id="p2-3"></a>
### P2-3 · Medium · A datetime's UTC offset is dropped

**Code path.** `fetch_rows()` reads through the reflected `DATETIME` type, which
parses `+02:00`/`Z` and discards the offset
([engine.py:722-724](../safeagentdb/engine.py#L722-L724)). The sandbox holds a
naive time, and a naive time is what is written back.

**Repro.** [p2/t4_tz.py](review-repros/p2/t4_tz.py) and
[p2/t5_copy_scalars_and_tz.py](review-repros/p2/t5_copy_scalars_and_tz.py)

```
sandbox: [{'id': 1, 'at': '2024-01-01 10:00:00.000000'}, {'id': 2, 'at': '2024-01-01 10:00:00.000000'}]
  prod (1,  ..., '2024-01-01 10:00:00+02:00')     prod (2,  ..., '2024-01-01T10:00:00Z')
  prod (11, ..., '2024-01-01 10:00:00.000000')    prod (12, ..., '2024-01-01 10:00:00.000000')
```

**Impact.** Two moments two hours apart look identical to the agent and to the
reviewer, and a copied or re-saved timestamp lands two hours off without a
warning. An unrelated edit leaves the stored value intact; a DELETE of such a row
hits P2-1.

---

## Piece 3a — error handling and API usage

### A. Exceptions that escape `SafeAgentDBError`

All from [p3a/e1_escapes.py](review-repros/p3a/e1_escapes.py) unless stated.

<a id="a1"></a>
#### A1 · Medium · Errors raised while `__enter__` reads the schema are not wrapped

**Code path.** `reflect_tables()` runs at
[sandbox.py:130-132](../safeagentdb/sandbox.py#L130-L132), before the `try`
that converts `SQLAlchemyError` into `SchemaError`. The CHANGELOG says "any
`SQLAlchemyError` during `__enter__` is now wrapped in `SchemaError`".

```
enter, unknown table: ESCAPES sqlalchemy.exc.InvalidRequestError: Could not reflect: requested table(s) not available ...
enter, production unreachable: ESCAPES sqlalchemy.exc.OperationalError: unable to open database file
```

**Impact.** The two most common setup failures, a mistyped table name and an
unreachable database, get past `except SafeAgentDBError`.

<a id="a2"></a>
#### A2 · Medium · Commit-time database errors are wrapped only for some changesets

**Code path.** Only statements sent through `_execute()` are wrapped
([sync.py:644-676](../safeagentdb/sync.py#L644-L676)). These go straight to
`conn.execute` and are not:

- `engine.begin()` ([sync.py:123](../safeagentdb/sync.py#L123))
- `_refuse_existing_key()` ([sync.py:425](../safeagentdb/sync.py#L425)), before every INSERT
- `_referencing_rows()` ([sync.py:369-384](../safeagentdb/sync.py#L369-L384)), the cascade check
- `_diagnose_conflict()` ([sync.py:702](../safeagentdb/sync.py#L702))

Production was held under `BEGIN EXCLUSIVE` by another connection:

```
commit, INSERT while prod locked: ESCAPES sqlalchemy.exc.OperationalError: database is locked
commit, UPDATE while prod locked: in hierarchy safeagentdb.errors.SyncError: The database rejected the UPDATE ... database is locked.
```

**Impact.** The same outage is caught or missed depending on whether the agent's
changeset starts with an INSERT. The README says "Every database error raised
while applying a changeset is wrapped". Nothing is written either way.

<a id="a4"></a>
#### A4 · Low · Raw errors after the agent changes the sandbox schema

**Code path.** `_take_snapshot()` selects through the clone-time `Table` objects
([sandbox.py:449-455](../safeagentdb/sandbox.py#L449-L455)).

```
diff, after RENAME COLUMN: ESCAPES sqlalchemy.exc.OperationalError: no such column: tasks.title
commit, after RENAME COLUMN: ESCAPES ... no such column: tasks.title
diff, after DROP TABLE: ESCAPES ... no such table: tasks
```

**Impact.** The agent can run any SQL. DDL leaves the review step with an
unexplained driver error instead of a clear refusal.

<a id="a5"></a>
#### A5 · Low · `execute()`/`query()` pass driver errors through unwrapped

**Code path.** [sandbox.py:214-228](../safeagentdb/sandbox.py#L214-L228) wraps the
string in an untyped `text()` and does not catch anything.

```
execute, Decimal bind: ESCAPES sqlalchemy.exc.ProgrammingError: type 'decimal.Decimal' is not supported
query, non-SELECT: ESCAPES sqlalchemy.exc.ResourceClosedError: This result object does not return rows.
(p1/f4) literal '{"a":1}': sqlalchemy.exc.StatementError: A value is required for bind parameter '1'
(p3a/a1 #15) agent statement violating a constraint: sqlalchemy.exc.IntegrityError
```

**Impact.** Errors in the agent's own SQL may well be meant to surface as they
are, but the README says "a whole agent session can be wrapped in a single
`except`". The `Decimal` and `:name` cases also fail on valid input. A colon
after a word character is not affected: `'12:30'` works.

<a id="a6"></a>
#### A6 · Low · Misuse errors are plain Python exceptions

`RuntimeError` when used before `__enter__` or after exit
([sandbox.py:445-447](../safeagentdb/sandbox.py#L445-L447)), `ValueError` for a
bad `on_conflict`, `AttributeError` when `changeset=` is not a `ChangeSet`.
Reasonable for programmer errors, but `RuntimeError` is easy to hit at runtime
and is not documented.

`pydantic.ValidationError` also escapes from `commit_to_production()`; the README
documents it.

### B. Error messages that name the wrong cause

<a id="b1"></a>
#### B1 · Medium · A string `tenant_id` makes every write a "Tenant breach"

**Code path.** The clone filter compares in SQL, where SQLite's type affinity makes
`'42'` match `42` ([engine.py:716-717](../safeagentdb/engine.py#L716-L717)).
`tenant_breach()` compares in Python with `!=`
([diff.py:596-611](../safeagentdb/diff.py#L596-L611)).

**Repro.** [p3a/m1_string_tenant.py](review-repros/p3a/m1_string_tenant.py)

```
clone_stats: {'tasks': 2} (the tenant filter matched)
is_valid: False
  blocking: Tenant breach blocked on DELETE: row in 'tasks' belongs to user_id=42, not '42'.
  blocking: Tenant breach blocked on UPDATE: row in 'tasks' has user_id=42, expected '42'.
commit: SyncError
```

**Impact.** Tenant IDs taken from a URL or a header are strings. Every write,
including a delete of the tenant's own row, is reported as a security breach
when the real cause is a type mismatch. It fails closed.

<a id="b2"></a>
#### B2 · Medium · A self-referencing table is written in key order

**Code path.** `_dependencies()` skips self-edges
([sync.py:535](../safeagentdb/sync.py#L535)), and within one table rows are
ordered by key ([sync.py:566-571](../safeagentdb/sync.py#L566-L571)).

**Repro.** [p3a/m2_self_reference.py](review-repros/p3a/m2_self_reference.py):
`emp.manager_id REFERENCES emp(id)`.

```
[delete manager and report] is_valid=True
  commit: IntegrityViolationError -> Production rejected the DELETE on 'emp' for row {'id': 1}: FOREIGN KEY constraint failed.
  ... the rule that rejected it is one the tenant-scoped clone could not evaluate.
[insert report before its manager's id] is_valid=True
  commit: IntegrityViolationError -> Production rejected the INSERT on 'emp' for row {'id': 10}: ...
```

**Impact.** Hierarchies (org charts, threaded comments, category trees) cannot be
changed when the keys are in the "wrong" order, and the message says the
sandbox could not evaluate a rule it did evaluate.

<a id="b7"></a>
#### B7 · Low · The validation message names only the field

**Code path.** `RowDiff.validate()` keeps the second line of the pydantic error
([diff.py:227-229](../safeagentdb/diff.py#L227-L229)), which is the field name.

**Repro.** [p3a/m3_validation_message.py](review-repros/p3a/m3_validation_message.py):
`status = 'yolo'` against `Literal["todo", "done"]` gives the message `'status'`.

**Impact.** The reviewer sees which field failed but not why.

### C. API usage

All from [p3a/a1_api_usage.py](review-repros/p3a/a1_api_usage.py).

<a id="c1"></a>
#### C1 · Low · `diff()` after a commit shows the committed changes as SAFE TO COMMIT

**Code path.** `diff()` does not check `_committed`, and the comparison snapshot
is not advanced after a commit ([sandbox.py:232-274](../safeagentdb/sandbox.py#L232-L274)).

```
4. commit without diff first: 1
6. diff after commit: {'INSERT': 0, 'UPDATE': 1, 'DELETE': 0}, is_valid=True, banner='[SAFE] AI CHANGES VERIFIED -- SAFE TO COMMIT'
```

**Impact.** A diff → approve → commit loop presents already-applied changes for
approval again. The second commit refuses with `SyncError`, so nothing is written
twice.

<a id="c2"></a>
#### C2 · Low · `__enter__` on an open sandbox discards the agent's work

**Code path.** No "already open" guard at
[sandbox.py:129](../safeagentdb/sandbox.py#L129).

```
13. nested enter: earlier edit still visible: [{'title': 'one'}] | old engine replaced: True
```

The edit (`'before'`) is gone, and the previous session and engine are never closed.

<a id="c3"></a>
#### C3 · Low · A re-entered committed sandbox can never commit

```
12. re-entered committed object, diff: {'INSERT': 0, 'UPDATE': 1, 'DELETE': 0}
    commit: in hierarchy SyncError: This sandbox has already been committed. Create a new ShadowDB.
```

The fresh clone accepts edits and diffs them, then refuses to commit. An object
that never committed can be re-entered and committed normally (#11). Not
harmful, but the README does not say whether re-entry is supported.

---

## Suspected, not reproduced

| Suspicion | Reasoning |
|-----------|-----------|
| P1-1 and P1-2 also occur on PostgreSQL | The delete-plus-insert translation and the statement order do not depend on the dialect. The Postgres inspector reports referential actions, so P1-4 should not apply there. Needs a server. |
| `timestamptz` on PostgreSQL shifts the instant | The driver returns aware datetimes; the sandbox stores them naive (`p2/t1` row 2 shows the offset gone in the sandbox even for an aware Python value). Writing one back into `timestamptz` would be read in the session time zone. |
| P2-1 does not affect PostgreSQL or MySQL | They compare typed values rather than text. Not verified. |
| A custom `row_key` with a generated PK gives a wrong-cause message | Deleting and re-inserting a row under the same `row_key` should produce an UPDATE whose `id` is a placeholder; `provisional_reference_columns()` would then say "references a row that is itself new". From reading the code only. |
| The cascade check fails on MySQL | `_referencing_rows()` builds SQL with double-quoted identifiers ([sync.py:361-381](../safeagentdb/sync.py#L361-L381)). Without `ANSI_QUOTES`, MySQL reads `"child"` as a string literal, so every cascade check would raise a raw `ProgrammingError` (see A2). Fails closed. |
| MySQL `TINYINT` columns fail strict validation | `TINYINT` maps to `String(255)` with a text conversion that never converts back ([engine.py:76](../safeagentdb/engine.py#L76), [engine.py:110-120](../safeagentdb/engine.py#L110-L120)). A strict `int` validator would see `'1'` and block every edit to the row. SQLite reflects `TINYINT` as `INTEGER`, so [p3b/c1_tinyint.py](review-repros/p3b/c1_tinyint.py) cannot reach this path. |
| The provisional-key commit path | Leaving the column out of the INSERT and reading the real key back was not run: a SQLite production table cannot usefully keep a `nextval()` default. The diff side was verified; `tests/test_server_backed.py` covers the commit side against PostgreSQL. |
| Compare-and-swap at "any isolation level" | Follows from the single-statement design; verified only by CI on PostgreSQL. |

---

## README claims audit (piece 3b)

Every guarantee in [README.md](../README.md), marked **Confirmed**, **Partial** or
**Not delivered**. Of the 44 rows in the claims table, 17 are confirmed, 17
partially delivered and 7 not delivered; 2 describe behaviour that has since
changed, and 1 groups disclaimers that are accurate.

### Tenant isolation

| When | Check | Code |
|------|-------|------|
| Clone | Rows read with `WHERE tenant_column = tenant_id`; a table with no tenant column reads nothing unless listed in `reference_tables` | [engine.py:713-724](../safeagentdb/engine.py#L713-L724) |
| Diff | `tenant_breach()` on every diff (INSERT/UPDATE new row, DELETE old row) → `blocking_errors` → `is_valid` false | [sandbox.py:457-477](../safeagentdb/sandbox.py#L457-L477), [diff.py:581-611](../safeagentdb/diff.py#L581-L611) |
| Diff | Any change to a reference table raises `SyncError` | [sandbox.py:518-531](../safeagentdb/sandbox.py#L518-L531) |
| Commit | Diff recomputed; `blocking_errors` raise `SyncError` | [sandbox.py:320-338](../safeagentdb/sandbox.py#L320-L338) |
| Commit | `tenant_breach()` again inside the transaction | [sync.py:132-134](../safeagentdb/sync.py#L132-L134) |
| Commit | `AND tenant_column = tenant_id` on every UPDATE/DELETE | [sync.py:205-207](../safeagentdb/sync.py#L205-L207) |
| Commit | Empty, unknown or mismatched row key refused, so a WHERE clause is never the tenant alone | [sync.py:749-786](../safeagentdb/sync.py#L749-L786) |
| Commit | Existing-key check for an INSERT scoped to the tenant | [sync.py:419-423](../safeagentdb/sync.py#L419-L423) |
| Commit | Cascading FKs checked in every production table; another tenant or no determinable owner → `CascadeError` | [sync.py:253-388](../safeagentdb/sync.py#L253-L388); `tests/test_review_findings.py:212-409` |

No reproduced finding writes to, or changes, another tenant's rows. P1-1
deletes rows through a cascade, but the cascade check works as designed: those
rows belong to the acting tenant. B1 blocks too much rather than too little.

### Claims

| Claim | Verdict | Evidence |
|-------|---------|----------|
| Gate 1: only tenant rows cloned; "other tenants' data never enters memory. Ever." | Partial | Filter confirmed. `reference_tables` loads whole tables (documented); the cascade check reads other tenants' tenant-column values into memory ([sync.py:379-388](../safeagentdb/sync.py#L379-L388)) |
| Gate 2: explicit INSERT/UPDATE/DELETE per row key; unidentifiable tables refused | Partial | Refusal confirmed ([sandbox.py:557-565](../safeagentdb/sandbox.py#L557-L565)); key change becomes DELETE + INSERT (P1-1) |
| Gate 3: every row validated, strict, no coercion | Partial | INSERT/UPDATE only; validator sees converted types (P2-2); terse message (B7) |
| Gate 4: row key + tenant in every WHERE; tenant-only WHERE refused | Confirmed | [sync.py:190](../safeagentdb/sync.py#L190), [sync.py:205-207](../safeagentdb/sync.py#L205-L207), [sync.py:764-770](../safeagentdb/sync.py#L764-L770) |
| Gate 5: compare-and-swap; `ConflictError` names drifted columns | Partial | Mechanism confirmed, real drift named correctly (p3a/a1 #16); false conflicts (P2-1, P1-2) |
| Gate 6: one transaction, deterministic order, changed columns only | Partial | Order deterministic but not always valid (P1-2, B2) |
| "Every gate that does not need production also runs inside `diff()` … cannot disagree" | Not delivered | FK ordering is checkable without production but is commit-only (P1-2, B2); banner and commit disagree in P1-1, P1-3, C1 |
| `__enter__` carries constraints, unique indexes and defaults | Partial | `tests/test_weaknesses.py:200,275`, `tests/test_hardening.py:231-278`; FK actions lost on SQLite production (P1-4) |
| `__enter__` raises `SchemaError` without a row key | Confirmed | [sandbox.py:557-565](../safeagentdb/sandbox.py#L557-L565) |
| `__exit__` destroys in-memory data | Confirmed | [sandbox.py:203-210](../safeagentdb/sandbox.py#L203-L210); p3a/a1 #14 |
| `row_key` must exist and be unique | Confirmed | [sandbox.py:545-555](../safeagentdb/sandbox.py#L545-L555), [sandbox.py:570-591](../safeagentdb/sandbox.py#L570-L591) |
| `on_conflict="ignore"` skips, records, warns | Confirmed | p1/f1b (the conflict itself was false, see P1-2) |
| `require_validators` policy matches between diff and commit | Confirmed | [diff.py:218-222](../safeagentdb/diff.py#L218-L222), [models.py:87-97](../safeagentdb/models.py#L87-L97) |
| Generated keys: placeholder, omitted at INSERT, real key read back | Confirmed by code | Diff side reproduced (p1/s_sound_checks); commit side [sync.py:172-185](../safeagentdb/sync.py#L172-L185), server-backed tests |
| Supplied key or placeholder reference → `GeneratedValueError` | Partial | Works; over-broad (P1-5) |
| Reference tables: full clone, FK restored, read-only | Confirmed | [sandbox.py:518-531](../safeagentdb/sandbox.py#L518-L531); `tests/test_audit_followups.py:125-193` |
| `unsupported_constraints` lists everything not reproduced | Partial | Lost FK actions not listed (P1-4) |
| `execute()` wraps SQL in `text()` | Confirmed | Side effect: `"a":1` parsed as a bind parameter (A5) |
| Commit returns rows actually written | Partial | Cascaded rows not counted (P1-1) |
| Commit's list of exceptions it raises | Partial | Omits `SchemaError` (FK cycle) and the raw exceptions in A2/P1-6 |
| Commit callable once | Confirmed | [sandbox.py:316-317](../safeagentdb/sandbox.py#L316-L317); p3a/a1 #5, #9 |
| `is_valid` true when nothing checkable without production is wrong | Not delivered | P1-2, B2, P1-3, C1 |
| `fingerprint` hashes exactly what would be written | Partial | Type collision (P1-6); counts a dropped change (P1-3) |
| Plain-text output off a TTY | Confirmed | [diff.py:297-312](../safeagentdb/diff.py#L297-L312); `tests/test_mega.py:632-651` |
| "Every one derives from `SafeAgentDBError` … a single `except`" | Not delivered | A1, A2, A4, A5, A6, P1-6 |
| "Every database error raised while applying a changeset is wrapped" | Not delivered | A2, P1-6 |
| `ConflictError` / `IntegrityViolationError` meanings | Partial | Also raised for causes they do not describe (P2-1, P1-2, B2) |
| `CascadeError` attributes; `ConflictWarning` per skip; `MissingValidatorError` is a `KeyError` | Confirmed | [sync.py:300-333](../safeagentdb/sync.py#L300-L333), [sync.py:474-491](../safeagentdb/sync.py#L474-L491), [errors.py:196](../safeagentdb/errors.py#L196) |
| Tenant scenarios 1–3 | Confirmed | Message wording differs slightly from the README |
| Any SQLAlchemy-supported database | Partial | SQLite and PostgreSQL (CI) only; MySQL suspected issues above |
| "Zero fidelity loss" | Not delivered | P2-2, P2-3, P1-3 |
| Catches schema violations; NOT NULL/PK/UNIQUE/CHECK/FK | Partial | P2-2, P1-4 |
| Catches column defaults; cross-tenant writes; unidentifiable rows | Confirmed | See above |
| Cascading keys checked at the tenant boundary | Confirmed | Same-tenant loss in P1-1 is not claimed otherwise |
| "Exactly what was reviewed" | Partial | P1-6, P1-1, P1-3 |
| Drift is a `ConflictError`, at any isolation level | Partial | No silent overwrite found; false conflicts (P2-1); isolation claim CI-only |
| Remapped columns: "Round-tripping is lossless" | Not delivered | P2-2, P1-3 |
| Unreliable-equality columns excluded from the guard | Partial | List incomplete: `DateTime`/`Boolean` on SQLite (P2-1) |
| Other "cannot catch" disclaimers | Accurate | Nothing contradicts them |
| FK enforcement suspended only during load | Confirmed | [engine.py:745-767](../safeagentdb/engine.py#L745-L767) |
| "ordered by table, row key, operation" | Out of date | Now phase, then topological table order, then key ([sync.py:541-573](../safeagentdb/sync.py#L541-L573)) |
| Any failure → full rollback | Confirmed | Every failed commit in this review left production untouched (`ignore` excepted, as documented) |
| "No raw SQL strings are generated" | Not delivered | The cascade check builds SQL with f-strings ([sync.py:361-381](../safeagentdb/sync.py#L361-L381)). Values are bound and the identifiers come from the schema, so not an injection risk |
| Architecture test list | Out of date | Omits `test_review_findings.py` and `test_server_backed.py` |

---

## Checked and found sound

**Keys and consistency** ([p1/s_sound_checks.py](review-repros/p1/s_sound_checks.py)):
composite-key updates, a change of one key component, swapping two rows' keys,
a plain PK change with no referencing rows, a key collision with another tenant
(refused with `IntegrityViolationError`, production untouched),
delete-and-re-insert with a new payload (one UPDATE of the changed column) and
with the same payload (empty), update-then-revert and insert-then-delete (empty),
placeholder keys and hand-picked keys on the diff side, a stale changeset refused
and accepted again once the extra change is undone, and a second commit refused.
In every case `is_valid` and the banner agreed with the commit.

**Types** ([p2/t1](review-repros/p2/t1_untouched_and_delete.py),
[p2/t2](review-repros/p2/t2_changed_values.py)): an edit to an unrelated column
preserves every other stored value and storage class exactly, across dates,
microsecond and offset datetimes, `Decimal` stored as REAL and as TEXT, booleans
`1` and `'true'`, bytes including `\x00`/`\xff`, the empty string, NULL, a 1 MB
string, nested JSON dicts and lists with nulls. A diff with no changes is empty
for all of them. Changed values are stored exactly as SQLAlchemy stores them
directly (date, datetime, `Decimal` passed as a string, `False`, bytes, NULL,
nested JSON, a JSON string, JSON set to NULL). DELETE works for every
SQLAlchemy-written row and for a `Decimal` stored as TEXT. The empty string and
NULL stay distinct.

**Lifecycle** ([p3a/a1_api_usage.py](review-repros/p3a/a1_api_usage.py)):
committing twice is refused inside and after the block; an exception inside the
`with` block propagates, writes nothing and closes the sandbox; a failed agent
statement mid-session leaves earlier work intact; a commit retried after a
resolved `ConflictError` applies exactly the agent's change; committing without
calling `diff()` first works; calls before `__enter__` or after exit are refused
rather than run against a closed engine.

**Wrapping that works**: a database error on the first UPDATE/DELETE
(`SyncError`), a production rejection (`IntegrityViolationError`), real drift
(`ConflictError` with the right columns), and the missing-validator,
generated-key, duplicate-key, changeset-mismatch and dependency-cycle errors.

**Not changed by this review:** `SQL NULL` and JSON `null` become the same value
in the sandbox, and an agent-written `NULL` reaches production as JSON `'null'`.
That matches SQLAlchemy's own `JSON` default (the control in `p2/t2` does the
same), so it is noted rather than filed.
