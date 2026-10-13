# Changelog

All notable changes to SafeAgentDB are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2026-10-13

A second safety release, built from two reviews.

An independent review of 0.2.0 found six issues; all six were reproduced
against the published package before anything was changed, and each is written
up in [docs/AUDIT.md](docs/AUDIT.md) with a failing-first test in
`tests/test_review_findings.py`.

A pre-release review of 0.3.0 itself found 20 more
([docs/REVIEW_0_3.md](docs/REVIEW_0_3.md)), each with a runnable reproduction in
`docs/review-repros/`. Every reproduction became a failing test in
`tests/test_review_0_3.py` before it was fixed, and the server-dependent ones
also run against PostgreSQL in `tests/test_server_backed.py`. The rule applied
throughout: where emulating database semantics correctly is hard, refuse
clearly rather than write something other than what was reviewed.

Several of these could destroy or misstate production data silently, so
upgrade if you use a custom `row_key`, have cascading foreign keys, change
primary keys, or store JSON or time-zoned timestamps.

**Supported databases are now stated plainly:** PostgreSQL is the first-class
production target, tested in CI. SQLite production is best-effort, with its
known limits documented in the README. MySQL is untested.

### Breaking changes (pre-0.3.0 review)

- **A row key changed with `UPDATE` is refused.** It used to be committed as a
  `DELETE` plus an `INSERT`, which ran production's `ON DELETE` actions instead
  of `ON UPDATE` ones: a routine key change deleted the tenant's child rows
  behind a SAFE banner. It is now a blocking error in `diff()` and a `SyncError`
  at commit. Change keys through the application.
- **A referential action on rows the diff does not show is refused by
  default.** A `DELETE` (or an `UPDATE` of a referenced column) that would make
  production run `CASCADE`, `SET NULL` or `SET DEFAULT` on existing rows raises
  `CascadeError`, even when those rows belong to the acting tenant. Pass
  `ShadowDB(..., allow_cascades=True)` to allow it within the tenant. A cascade
  the diff already shows, because the child table is cloned, needs no opt-in.
- **Statement order is unit-of-work order**: INSERTs parents first, then
  UPDATEs, then DELETEs children first, except where a foreign key between two
  rows of the changeset needs otherwise, in any table and any phase (including
  a foreign key onto a `UNIQUE` column that is not the primary key). DELETEs
  used to run first, so re-pointing a child and then deleting its old parent
  never committed. A cycle between tables is no longer refused outright; rows
  with no order production would accept one statement at a time are refused in
  `diff()`, including a cloned child that follows its parent's new value
  through `ON UPDATE CASCADE`.
- **`commit_to_production()` raises `RowValidationError`** (a `SyncError`) for a
  row that fails its `SafeModel`, instead of pydantic's `ValidationError`, which
  is kept as `__cause__`. `except pydantic.ValidationError` around a commit
  must become `except RowValidationError`. `safeagentdb.models.validate_row()`
  still raises pydantic's error.
- **`execute()` and `query()` raise `AgentSQLError`** for a failing statement,
  instead of the raw SQLAlchemy error (kept as `__cause__`). `query()` on a
  statement that returns no rows raises it too.
- **`execute()` without `params` sends the SQL to SQLite as written**, with no
  `text()` bind-parameter parsing, so `'{"a":1}'` is a literal. With `params`
  it still uses `text()`.
- **Misuse raises `UsageError`**, which subclasses `RuntimeError` (what most of
  these cases raised before). A bad `on_conflict` used to raise `ValueError`,
  and `changeset=` that is not a `ChangeSet` raised `AttributeError`. New
  `UsageError` cases: `diff()` after a successful commit, entering a sandbox
  that is open, and entering one that has committed.
- **JSON values are held as their JSON encoding in the sandbox**, scalars
  included. Text an agent writes into a JSON column must be JSON (`'"hello"'`
  for a string); text that is not is refused, where it used to be written as a
  string.
- **Datetimes are held as ISO 8601 text in the sandbox**, keeping a UTC offset.
  `RowDiff.old`/`new` hold that text; `RowDiff.logical_row()` gives the
  `datetime`. A value with an offset for a column without time zone (PostgreSQL
  `timestamp`) is refused, where the database used to convert it silently.
- **`tenant_id` is converted to the tenant column's type at `__enter__`**, so
  `ShadowDB.tenant_id` may differ from what you passed (`"42"` becomes `42`, and
  `42` becomes `Decimal("42")` for a `NUMERIC` column). A value that does not
  convert exactly raises `UsageError`. A referencing row counts as the acting
  tenant's only when its tenant value equals the tenant id exactly, so `'042'`
  in a `TEXT` column is another tenant's.
- **On a SQLite production, `DATETIME`, `DATE`, `TIME` and `BOOLEAN` columns are
  no longer part of the compare-and-swap guard**, so a concurrent edit confined
  to one is not detected there.

### Breaking changes (second review)

- **`ChangeSet.is_valid` is stricter.** A tenant breach, a row key that no
  longer identifies a single row, or a write to a reference table now make it
  `False`. 0.2.x reported those only at commit, so code branching on
  `if changeset.is_valid:` will take the other path for changesets it used to
  accept — which is the point: the commit was going to refuse them anyway.

- **`commit_to_production()` raises in cases it previously allowed.**
  `DuplicateRowKeyError` when an INSERT carries a key production already holds,
  and `CascadeError` when a cascading foreign key would reach another tenant.
  Both were silent data loss before.

- **Statement order changed.** Tables are now ordered by foreign-key dependency
  rather than alphabetically. If you depended on the old order, you depended on
  a bug.

- **`IntegrityViolationError`'s message no longer claims a cause.** It used to
  say the rejection happened "because it holds only this tenant's rows" for
  every integrity error, including ones that had nothing to do with tenancy.
  Code matching on that text needs updating.

- `safeagentdb.__all__` grew: `CascadeError`, `ChangesetMismatchError`,
  `DuplicateRowKeyError`.

### Added

- **`commit_to_production(changeset=...)`** — pass the `ChangeSet` you reviewed
  and the commit recomputes the diff, comparing fingerprints, and refuses with
  `ChangesetMismatchError` if the sandbox changed since. This is the recommended
  form wherever a human or a second agent approves the change; without it,
  anything that happened after the review is committed too. Calling with no
  argument behaves as in 0.2.x.
- `ChangeSet.fingerprint` — a content-addressed, order-independent hash of
  exactly what the changeset would write.
- `ChangeSet.blocking_errors` — problems found without touching production, which
  the commit will refuse. Rendered under `BLOCKED BEFORE COMMIT` in both the Rich
  and plain output.
- `ShadowDB.cascade_references` — the cascading foreign keys that point into the
  cloned tables.
- `CascadeError` (with `.tenants` and `.unattributed_rows`),
  `ChangesetMismatchError` and `DuplicateRowKeyError`.
- `diff.tenant_breach()` and `RowDiff.logical_row()`, the shared helpers that
  keep `diff()` and the commit from disagreeing.
- `sync.table_write_order()`, the topological table sort, which raises on a
  dependency cycle.
- Eight more server-backed tests, bringing that suite to 20 against PostgreSQL
  in CI.
- From the pre-0.3.0 review: `ShadowDB(allow_cascades=...)`; `AgentSQLError`,
  `RowValidationError` and `UsageError`; `ChangeSet.blocking_errors` entries for
  key changes and row cycles; `diff.canonical_value()`, the type-tagged form
  behind change detection and the fingerprint; `models.format_validation_error()`.
  72 tests in `tests/test_review_0_3.py`, 47 in `tests/test_review_round4.py`
  and 15 more server-backed tests: 327 local tests and 35 against PostgreSQL.

### Fixed (pre-0.3.0 review)

Ids refer to [docs/REVIEW_0_3.md](docs/REVIEW_0_3.md).

- **P1-1** A primary-key change was committed as `DELETE` + `INSERT`. Refused,
  and same-tenant referential actions need `allow_cascades=True` (see above).
- **P1-2 / B2 / B3** All DELETEs ran before any UPDATE, and a self-referencing
  table was written in key order, so valid changesets failed with messages that
  blamed a concurrent writer or a rule the sandbox "could not evaluate". Now
  unit-of-work order with row-level ordering, and neither message is used for
  an ordering failure.
- **P1-3** A JSON change Python considers equal (`1` to `true`) was shown as an
  UPDATE and never written, and a formatting-only rewrite showed as an empty
  UPDATE. Changes are compared in a type-tagged canonical form.
- **P1-4** With SQLite production the sandbox dropped `ON DELETE`/`ON UPDATE`
  actions, refusing deletes production accepts. They are read from
  `PRAGMA foreign_key_list` and carried into the sandbox.
- **P1-5 / B5** Any integer at or above 2^52 in any column was treated as a
  placeholder key. Generated keys are now tracked; only keys the sandbox
  generated are placeholders. Also fixed: a row deleted and re-inserted under a
  custom `row_key` could write a placeholder into a real id.
- **P1-6 / A3** The fingerprint collided across value types, and a bind-time
  error at commit escaped as a raw `StatementError`. Type-tagged fingerprint;
  every commit error is wrapped.
- **P2-1 / B6** False `ConflictError` on a SQLite production for a datetime or
  boolean stored in another text format, blaming a writer that did not exist.
  Those types are left out of the guard on SQLite, and a conflict with no
  drifted column no longer claims another writer.
- **P2-2** JSON scalars changed type in the sandbox (`"123"` became `123`,
  `true` became `1`, `"null"` became `null`).
- **P2-3** A datetime's UTC offset was dropped in the sandbox and written back
  without it. Offsets survive, including PostgreSQL `timestamptz`.
- **A1** Errors while `__enter__` read the schema (unknown table, unreachable
  database) escaped unwrapped. They are `SchemaError` now -- which the entry
  below already claimed for this release, and is now true.
- **A2** Commit-time database errors were wrapped only for some changesets.
  Every database error during a commit is a `SyncError`.
- **A4** Schema changes by the agent made `diff()` and the commit fail with raw
  driver errors. They raise `SchemaError`.
- **A5** `execute()`/`query()` failed on valid input (`Decimal` parameters,
  `'{"a":1}'` literals) and passed driver errors through.
- **A6** Misuse raised plain `RuntimeError`/`ValueError`/`AttributeError`.
- **B1** A string `tenant_id` for an integer column made every write a "tenant
  breach".
- **B4** An error named a `DELETE` the agent never issued (the P1-1 key change).
- **B7** The validation message named only the field; it now gives every
  failing field, pydantic's reason and the value.
- **B8 / C1** `diff()` after a commit showed the committed changes as SAFE TO
  COMMIT.
- **C2** Entering an open sandbox discarded the agent's work and leaked the
  engine.
- **C3** Re-entering a committed sandbox opened one that could never commit.
  Re-entry is now documented: allowed until a commit, refused after.
- The cascade check is built from SQLAlchemy Core rather than f-string SQL, so
  it no longer double-quotes identifiers (suspected to fail on MySQL).
- MySQL `TINYINT` is an integer in the sandbox, not a string.

### Fixed (pre-0.3.0 review, round 4)

A review of the fixes above (Round 4 in
[docs/REVIEW_0_3.md](docs/REVIEW_0_3.md)). Each reproduction became a failing
test in `tests/test_review_round4.py` before it was fixed.

- **R4-1** The cascade check counted another tenant's rows as the acting
  tenant's when a `TEXT` tenant column held `'042'`, `'+42'` or `' 42'`, so
  `allow_cascades=True` let production delete them. Tenant values are now
  compared exactly, after a lossless conversion only.
- **R4-2** A `NUMERIC`/`DECIMAL` tenant column refused an integer `tenant_id`.
  Any exact conversion between `int`, `Decimal` and integer text is taken.
- **R4-3** UPDATEs were not ordered by the references between rows, so a
  changeset re-pointing a child and renaming its parent's `UNIQUE` value, or
  renaming a value and inserting a child that references it, was shown as SAFE
  and then rejected by production.
- **R4-4** A key the agent set by `UPDATE` on its own new row was treated as one
  the sandbox generated, and production replaced it. It is refused as a
  supplied value, exactly like the same key set by `INSERT`.
- **R4-6** Deleting a row, inserting a new one under its key and moving that was
  reported as a key change of the deleted row.
- **R4-8** `ANALYZE` was refused as a schema change.
- **R4-9** `AgentSQLError` said earlier changes were kept after `OR ROLLBACK`
  had discarded them. It now says which happened.
- A datetime with an offset for a PostgreSQL `timestamp` column was converted by
  the session time zone without notice. It is refused.
- Documented, not changed: an upsert on the agent's own new row in a table
  whose key production assigns is refused (R4-5), and a forged row inserted
  into the key-change log can hide a key change (R4-7). See the README's known
  limits.

### Fixed (second review)

- **A duplicate custom row key overwrote a production row.** Two sandbox rows
  sharing a `row_key` were folded into one diff entry by a dict comprehension,
  reported as a single UPDATE with `is_valid` `True`, and committed as an
  overwrite: two rows in, one row out, holding the wrong payload. Uniqueness was
  only ever checked at `__enter__`. Now re-checked on the current sandbox state
  in `diff()`, guarded in `compute_diff()`, and refused at commit.
- **`ON DELETE CASCADE` crossed the tenant boundary.** Deleting a parent owned by
  one tenant also deleted another tenant's child, with nothing recorded or
  warned. Cascading keys across *all* of production are now found at open and
  checked at commit. A referencing table with no tenant column blocks the write
  too, reported as `CascadeError.unattributed_rows`, since an owner that cannot
  be determined must not be read as the acting tenant. SQLAlchemy's SQLite
  inspector does not report referential actions, so `PRAGMA foreign_key_list`
  is used there.
- **A populated `JSON` column crashed the sandbox open** with
  `sqlite3.ProgrammingError: type 'dict' is not supported`. Remapped columns now
  carry a conversion in each direction, and any `SQLAlchemyError` during
  `__enter__` is wrapped in `SchemaError` rather than escaping raw.
- **`diff()` reported `SAFE TO COMMIT` for a change the commit rejected.** The
  tenant guard was commit-only. It, the reference-table guard and the row-key
  checks now run in `diff()` through the same helpers sync uses.
- **The changeset a reviewer approved was not necessarily the one committed.**
  See `commit_to_production(changeset=...)` above.
- **Statement order ignored foreign-key dependencies**, so a child could be
  inserted before its parent and rejected. Ordering follows foreign keys now
  (refined to row level by the pre-0.3.0 review, above), and rows that
  reference each other in a cycle are refused rather than attempted.

## [0.2.0] - 2026-09-20

A safety release. Every item under **Fixed** is a case where 0.1.x could lose or
corrupt production data while reporting success. The four findings are written
up in [docs/AUDIT.md](docs/AUDIT.md) and each has a regression test in
`tests/test_weaknesses.py`. Five further issues found while reviewing that work
are fixed here too, pinned by `tests/test_audit_followups.py`.

If you are on 0.1.x, read **Breaking changes** before upgrading.

### Breaking changes

- **Tables without a primary key are now rejected.** `ShadowDB.__enter__` raises
  `SchemaError` naming the table. Previously such a table was accepted and then
  silently corrupted. Pass the new `row_key` option to keep using it:

  ```python
  ShadowDB(engine, tables=["events"], tenant_id=42,
           row_key={"events": ["tenant_id", "event_uuid"]})
  ```

- **A table with no registered `SafeModel` now makes `diff()` report the
  changeset as invalid.** `changeset.is_valid` flips from `True` to `False` and
  the dashboard shows `[BLOCKED]` where 0.1.x showed `[SAFE]`, so any code
  branching on `if changeset.is_valid:` takes the other path. This is the part
  that changes control flow silently -- check it before upgrading.

  The commit side is *not* newly broken: 0.1.x already raised `KeyError` there,
  and `commit_to_production()` now raises `MissingValidatorError`, which
  subclasses `KeyError`, so existing handlers keep working. Pass
  `require_validators=False` for the old lenient behaviour, which now warns
  consistently on both sides instead of passing on one and raising on the other.

- **`commit_to_production()` can now raise `ConflictError`** when a production
  row changed between clone and commit. Previously the change was overwritten
  without a word. Pass `on_conflict="ignore"` to skip drifted rows instead --
  which applies the changeset in part and records every skip.

- **A `serial`/identity key is now assigned by production, not by the sandbox.**
  The column is left out of the `INSERT` and the real key is read back into
  `ShadowDB.assigned_keys`; the diff shows `(assigned by production)` instead of
  a placeholder. Supplying the key by hand is refused with
  `GeneratedValueError`, as is a new row referencing another new row's
  placeholder. 0.1.x wrote a key the sequence had never issued.

- **A foreign key whose parent table clones zero rows is no longer enforced** in
  the sandbox, and is listed in `unsupported_constraints`. Pass
  `reference_tables=[...]` to clone shared lookup tables in full and get the
  constraint back. Those tables are read-only.

- **The return value of `commit_to_production()` changed meaning.** It is now the
  real number of rows written, summed from each statement's rowcount, rather
  than the number of diffs attempted. A statement matching zero rows is a
  conflict, not a success.

- **`UPDATE` now writes only the columns the agent changed**, not every column.
  If you relied on a commit rewriting a whole row, it no longer does.

- **The sandbox now enforces foreign keys** (`PRAGMA foreign_keys=ON`). Sandbox
  operations that previously produced orphan rows now fail there, as they would
  in production.

- `validate_row()` raises `MissingValidatorError` instead of a bare `KeyError`.
  `MissingValidatorError` subclasses `KeyError`, so existing `except KeyError`
  handlers keep working.

- `safeagentdb.__all__` grew: `SafeAgentDBError`, `SchemaError`, `SyncError`,
  `ConflictError`, `GeneratedValueError`, `IntegrityViolationError`,
  `MissingValidatorError`, `MissingValidatorWarning`, `ConflictWarning`,
  `SkippedConflict` and `AssignedKey`.

### Added

- `row_key` option: an explicit row-identifying key per table. Its columns must
  exist and must be unique across the cloned rows, or `SchemaError` is raised.
- `reference_tables` option: clone the named tables in full, ignoring the tenant
  filter, so foreign keys pointing at shared lookup tables stay enforced. Their
  rows are visible to the agent and they are read-only.
- `ShadowDB.reference_table_names`, `ShadowDB.generated_columns` and
  `ShadowDB.skipped_conflicts`.
- `GeneratedValueError`, `IntegrityViolationError`, `SkippedConflict`,
  `AssignedKey` and `ConflictWarning`.
- `ShadowDB.assigned_keys` and `ShadowDB.provisional_key_columns`.
- A `requires_db` pytest marker and `tests/test_server_backed.py`, run by CI
  against a PostgreSQL 16 service container and skipped when `DATABASE_URL` is
  unset.
- `on_conflict` option: `"abort"` (default) or `"ignore"`.
- `require_validators` option: `True` (default) or `False`.
- `ShadowDB.row_keys` — the row-identifying columns in use per table.
- `ShadowDB.unsupported_constraints` — schema elements that could not be
  reproduced in the sandbox and are therefore not enforced there. Rendered in
  both the Rich and plain diff output under a `NOT ENFORCED IN SANDBOX` heading.
- `safeagentdb.errors` module with a `SafeAgentDBError` base class:
  `SchemaError`, `SyncError`, its subclasses `ConflictError`,
  `GeneratedValueError` and `IntegrityViolationError`, `MissingValidatorError`,
  the `SkippedConflict` record, and the `MissingValidatorWarning` and
  `ConflictWarning` warning categories.
- `ConflictError` carries `.table`, `.row_key` and `.columns`.
- Hard guard in `sync.apply_changeset`: an `UPDATE`/`DELETE` with an empty key,
  unknown key columns, or a key that disagrees with the declared `row_key`
  raises `SyncError` instead of executing.
- GitHub Actions CI running pytest on Python 3.10, 3.11 and 3.12, plus ruff.
- `tests/test_weaknesses.py`, `tests/test_hardening.py`,
  `tests/test_audit_followups.py` and `tests/test_server_backed.py`
  (188 tests total; the 12 server-backed ones need `DATABASE_URL`).

### Fixed

- **Primary-key-less tables corrupted data.** `_pk_key()` returned the empty
  tuple for every row, so `compute_diff` collapsed a whole table into one entry
  and `apply_changeset` emitted `UPDATE ... WHERE <tenant column>` with no row
  predicate. One agent edit rewrote every row the tenant owned; an edit to any
  other row vanished from the diff entirely.
- **The sandbox did not enforce most of the schema.** For any non-SQLite
  production database the clone rebuilt each column from name, type,
  primary_key and nullable only, dropping `UNIQUE`, `CHECK`, foreign keys,
  unique indexes and server defaults. Which tables lost them depended on
  `_detect_dialect` sniffing column type modules, so adding one `JSONB` column
  silently changed the fidelity of a whole table. The clone now starts from
  `Table.to_metadata()` for every dialect and rewrites only what SQLite cannot
  express.
- **Server defaults are translated rather than dropped**: `::type` casts are
  stripped, `now()` becomes `CURRENT_TIMESTAMP`, `true`/`false` become `1`/`0`.
  A default with no SQLite equivalent (`nextval(...)`, `gen_random_uuid()`) is
  removed and recorded in `unsupported_constraints`.
- **Concurrent production writes were silently lost.** The clone-time values are
  now carried in the `WHERE` clause of the `UPDATE`/`DELETE` itself, so checking
  and writing are one atomic statement with no window between them, at any
  isolation level and without taking row locks. An `UPDATE` guards the columns
  the agent changed, so unrelated concurrent edits coexist; a `DELETE` guards
  the whole row. `NULL` guards use `IS NULL`. Columns whose equality does not
  survive a driver round-trip -- float, JSON, array, binary -- are excluded.
  Statements run in a deterministic row order so two changesets cannot deadlock
  against each other.
- **Database errors escaped the exception hierarchy.** An `IntegrityError` from
  production -- a `UNIQUE` collision with another tenant's row, say -- is now
  wrapped in `IntegrityViolationError` with the original as `__cause__`, so a
  single `except SafeAgentDBError` catches everything the README claims.
- **`on_conflict="ignore"` applied changesets in silence.** Every skipped row is
  now recorded in `ShadowDB.skipped_conflicts` and warned about.
- **`RowDiff.validate()` and `sync.validate_row()` disagreed** about a table
  with no validator, so a changeset could show `[SAFE]` and then abort the
  commit. Both now share one policy and one message.
- **A `CHECK` constraint on a SQLite production table could crash sandbox
  creation** with a raw `OperationalError`. SQLAlchemy 2.0.35 and earlier
  reflect an unbalanced expression when the table's closing paren sits on the
  same line as the `CHECK`; later versions do not. Such a constraint is now
  skipped and recorded whatever produced it, and any other DDL failure raises
  `SchemaError`. SafeAgentDB behaves the same on either SQLAlchemy, so no
  minimum beyond the existing `sqlalchemy>=2.0` is needed.
- **A foreign key pointing at a table outside the cloned set** made
  `MetaData.sorted_tables` raise `NoReferencedTableError`. Such keys are now
  removed and recorded.
- Row loading is ordered parents-before-children, and foreign key enforcement is
  suspended only for the duration of the copy, since a tenant-scoped clone is a
  partial view of production.
- `ShadowDB.__enter__` disposes the sandbox engine if setup fails partway.

## [0.1.2] - 2026-09-20

- Use raw GitHub links in the README so images render on PyPI.

## [0.1.1] - 2026-09-20

- Documentation and safety screenshots.

## [0.1.0] - 2026-09-20

- Initial release: tenant-scoped SQLite sandbox, row-level diff dashboard,
  Pydantic validation and atomic sync to production.
