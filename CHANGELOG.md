# Changelog

All notable changes to SafeAgentDB are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
  creation** with a raw `OperationalError`. SQLAlchemy's SQLite reflection
  returns an unbalanced expression when the table's closing paren sits on the
  same line as the `CHECK`. Such a constraint is now skipped and recorded;
  any other DDL failure raises `SchemaError`.
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
