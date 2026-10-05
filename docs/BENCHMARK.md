# SafeAgentDB agent benchmark

**Status: harness built, not yet run. Every number below is a placeholder.**
Nothing in this document is a measurement until the table in
[Results](#results) is filled in from a real run, and the raw per-task JSON is
committed alongside it.

## The question

An LLM agent with write access to a production database will sometimes write
something it should not. Two numbers matter:

1. **How often does that happen?** Measured without SafeAgentDB in the path.
2. **How much of it does SafeAgentDB keep out, and what does it cost in false
   alarms?** Measured with SafeAgentDB in the path, on the *same* SQL.

A block rate on its own is not an answer. A layer that refuses everything blocks
100% of unsafe writes and is useless, so the false-positive rate is reported
next to it and the task suite is deliberately about half legitimate work.

## Method

### The database

`benchmark/seed.py` builds a file-based SQLite database from scratch before
every run — no timestamps, no randomness, byte-identical each time, so a
difference between runs is a difference in agent behaviour and nothing else.

```
plans    code, label, monthly_cents          shared reference data, NO tenant column
users    id, tenant_id, email, plan_code     email UNIQUE across all tenants
tasks    id, tenant_id, owner_id, title,     status enum (CHECK), priority 0..5 (CHECK),
         status, priority                    owner_id FK -> users.id
```

Two tenants: **42**, which agents act for, and **99**, which they must never
touch. Four users and six tasks, at fixed ids.

The shape is chosen so each layer has something to catch: an enum and a range
the schema can express, a cross-tenant boundary it cannot, a UNIQUE constraint
whose colliding row belongs to the *other* tenant (invisible to a tenant-scoped
sandbox), and a shared lookup table that no single tenant may edit.

### The agent

`benchmark/agent.py` is one agent, used by both arms. It is given the schema,
the seed rows and the task in natural language, and returns SQL through a
`submit_sql` tool call. Model, effort, temperature and max_tokens are
configurable and recorded in every result file.

It is called **once per task**. The same statements then run in both arms, so
the only difference between the arms is where the SQL lands — not what the model
decided to write. The cost of that choice is that the agent cannot inspect the
database before writing; see [Limitations](#limitations).

Current Claude models removed sampling parameters, so `temperature` returns a
400 and `output_config.effort` is the depth control. The harness passes
temperature only if you set it, and raises a readable error if the model refuses
it.

### The two arms

Both arms get their own fresh copy of the seeded database.

**Arm A — control.** The statements go straight at the database, the way an
agent holding a connection and no safety layer would. The database still
enforces its own constraints, with `PRAGMA foreign_keys=ON`. **This arm is "no
SafeAgentDB", not "no protection".** Anything the schema can express — the
status enum, the priority range, the FK, the UNIQUE email — is still rejected
here, and the report counts those separately as *database-rejected* rather than
crediting them to anyone. What is left over is what the schema cannot express:
tenant scope, bulk blast radius, shared reference data.

**Arm B — SafeAgentDB.** The same statements run inside `ShadowDB`, scoped to
the task's tenant, with `plans` as a `reference_tables` entry. Then `diff()`,
then `commit_to_production()`. The result records which stage stopped it:

| `blocked_at` | Meaning |
|---|---|
| `open` | The sandbox refused to be created at all. |
| `sandbox` | The statement failed inside the sandbox — a constraint the clone reproduced. |
| `diff` | The changeset was rejected before any write, e.g. a read-only reference table. |
| `commit` | Validation, tenant guard, or production itself refused the write. |

### The auditor

`benchmark/audit.py` decides whether a database ended up broken. It compares the
final state against the seed with plain SQL over stdlib `sqlite3`, and checks:

- `invalid_status`, `invalid_priority` — values outside the enum and the range
- `type_violation` — wrong storage class, via `typeof()`
- `null_violation`, `duplicate_email`, `orphan_reference`
- `cross_tenant_update` / `_delete` / `_insert`, `tenant_reassigned`
- `reference_data_update` / `_insert` / `_delete` — the shared `plans` table
- `tenant_table_emptied` — the acting tenant lost a whole table

### Why the auditor is independent of the validators

This is the part that decides whether the benchmark means anything.

Arm B validates rows against `SafeModel` schemas in
`benchmark/validators.py`. If the auditor imported those same schemas, then any
invariant missing from them would be missing from the audit too — a write that
broke a rule nobody had written down would be scored as safe by both the library
and its own scorecard. The benchmark would be marking its own work, and the
block rate would approach 100% by construction.

So the auditor:

- does not import `safeagentdb`, and does not import `benchmark.validators`;
- does not import SQLAlchemy — it uses `sqlite3` and SQL strings;
- **restates the allowed status values and the priority range in its own
  source**, duplicated on purpose. If someone widens the enum in the validator
  and forgets the auditor, the benchmark fails loudly instead of quietly
  agreeing with itself.

`audit.py` enforces the first two points on itself: `verify_independence()`
reads the module's own import lines and raises if any forbidden name appears.
`run.py` calls it before the first task, so a future refactor that couples them
stops the benchmark rather than skewing it.

The auditor still encodes *somebody's* judgement about what "broken" means —
mine. That is a real limitation, not a solved problem; see below.

## Reproducing

```bash
pip install -e ".[benchmark]"
export ANTHROPIC_API_KEY=...
python -m benchmark.run --runs 3 --model claude-opus-5-5 --effort medium
python -m benchmark.report
```

`run.py` writes `benchmark/results/raw-<stamp>.json` with every statement, every
exception and every violation. `report.py` writes
`benchmark/results/<stamp>.json` and prints the table. Both are committed with
any published result, so the numbers can be re-adjudicated without re-running
the model.

## What a run costs

One API call per task per run: `calls = tasks × runs`. 20 tasks over 3 runs is
**60 calls**.

The system prompt — instructions plus schema plus seed rows, about 1.5k tokens —
is identical for every task and marked for caching, so after the first call it
is a cache read. Per-task input is ~150 tokens. Output dominates the bill
because thinking is on by default: budget roughly 600–1,000 output tokens per
call at `--effort medium`, less at `low`, considerably more at `high`.

Estimated cost for **20 tasks × 3 runs = 60 calls**, at ~800 output tokens each:

| Model | Input $/MTok | Output $/MTok | Estimated run |
|---|---|---|---|
| `claude-opus-5-5` (default) | $4.00 | $20.00 | **~$1.05** |
| `claude-sonnet-5-5` | $2.00 | $10.00 | **~$0.55** |
| `claude-haiku-4-5` | $1.00 | $5.00 | **~$0.27** |

Scale linearly with task count. These are arithmetic from the published rates,
not measured — `report.py` prints the actual token counts from each run, so
replace them with real figures once you have some.

A dry run (`--dry-run`) costs nothing and exercises both arms with fixed SQL.

## Results

Not yet run.

- Model: _not yet run_
- Effort: _not yet run_
- Date: _not yet run_
- SafeAgentDB version: _not yet run_
- Tasks: _not yet written_ · Runs: _not yet run_

| Metric | Mean ± SD | Denominator |
|---|---|---|
| Agent produced an unsafe write | | |
| …of which the database itself rejected | | |
| SafeAgentDB block rate | | |
| Violations surviving Arm B | | |
| False-positive rate (safe tasks blocked) | | |
| Safe tasks left broken by Arm B | | |
| Agent returned SQL | | |
| Agent refused the request | | |

| Stage | Mean ± SD per task |
|---|---|
| Agent call | |
| Arm A execution | |
| Arm B execution | |

### How to read these

- **Agent produced an unsafe write** is the denominator for the block rate. A
  task the agent declined, or quietly did correctly, is not a task SafeAgentDB
  saved, and counting it would inflate the result.
- **SafeAgentDB block rate** is over exactly those writes that broke production
  in Arm A. 100% here means nothing got through that otherwise would have.
- **Violations surviving Arm B** is the number that matters most if it is not
  zero. It means a write broke an invariant *with* the library in the path.
- **False-positive rate** is the price. Read it with the block rate or neither
  figure means anything.

## Limitations

**The stand-in production database is SQLite.** Chosen so a run needs no server
and no Docker. Consequences: there are no sequences, so the provisional-key path
never engages and `GeneratedValueError` is never exercised; there is no real
concurrency, so the compare-and-swap conflict detection has nothing to detect
and `ConflictError` never fires; and SQLite's dynamic typing and collation
differ from PostgreSQL's. Those paths are covered by
`tests/test_server_backed.py` against a real PostgreSQL in CI, not here.

**The control arm is not unprotected.** The database enforces its own
constraints in Arm A. The benchmark therefore measures what SafeAgentDB adds
*on top of a correctly constrained schema*, which is the honest comparison but a
narrower one than "agent with vs. without guardrails". Tasks whose unsafe
outcome the schema can already reject will show up as database-rejected in both
arms; that is reported, not hidden.

**The agent gets one shot.** It cannot run a `SELECT` before writing, because
letting it read would show it different data in each arm and break
comparability. Real agents iterate, and an iterating agent would likely make
different — plausibly fewer — mistakes. This measures one-shot SQL authoring.

**The task suite is written by the library's author.** Both the tasks and the
ground-truth labels come from the same person who wants the library to look
good. There is no way to remove that bias from inside the repository. What is
done instead: prompts and labels live in one separate file, the auditor shares
no code with the library, and the raw per-task JSON is published so anyone can
re-label a row they disagree with and recompute.

**The auditor encodes one person's invariants.** `tenant_table_emptied` is the
clearest judgement call: if an agent deletes every one of a tenant's own tasks
from a vague prompt, SafeAgentDB applies it — those are the tenant's rows and
the library does not read intent — and the auditor calls it broken. Such a row
appears as a violation surviving Arm B. That is a genuine limit of the library,
not a harness bug, but it is a limit the auditor's author chose where to draw.
Check the per-task records before reading any such number as a failure.

**Three runs is not a confidence interval.** The standard deviation across three
repeats of a suite of a few dozen tasks indicates whether a result is stable. It
is not a statistical claim, and no number here should be quoted with more
precision than one decimal place.

**Model output is not deterministic and cannot be pinned.** Current Claude
models removed `temperature`, so there is no sampling control to freeze. Effort
is recorded; runs still vary. Results are tied to one model at one date and will
drift as models change.

**Prompt phrasing moves the numbers.** Rewording a task can flip whether the
agent writes the unsafe SQL at all. The suite tests the agent's judgement on
particular phrasings, not an intrinsic property of the model.

**Latency is reported, not compared.** Arm B clones a tenant's rows, diffs them
and re-reads each target row; it does strictly more work than Arm A by design.
The figures are there to show the overhead is bounded, not to argue it is free.
