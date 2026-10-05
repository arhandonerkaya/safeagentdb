# benchmark/

How many unsafe writes does a real LLM agent push to production, and how many
does SafeAgentDB keep out? Method, arms and limitations are in
[../docs/BENCHMARK.md](../docs/BENCHMARK.md). This file is just how to run it.

## Install

```bash
pip install -e .
pip install -r benchmark/requirements.txt
export ANTHROPIC_API_KEY=...
```

## Write the tasks

`tasks.yaml` ships with three entries marked `EXAMPLE ... REPLACE ME`. Replace
them. The field reference and the labelling rules are in the comments at the top
of that file; `expected` is the ground truth and nothing else in the harness
knows it.

## Run

```bash
python -m benchmark.run --dry-run            # wiring check, no API calls
python -m benchmark.run --runs 3             # the real thing
python -m benchmark.report                   # aggregates the newest raw file
```

`run.py` writes `results/raw-<stamp>.json` (every per-task detail);
`report.py` writes `results/<stamp>.json` (the aggregate) and prints the
markdown table.

One API call per task per run, so `--runs 3` over 20 tasks is 60 calls. See
[../docs/BENCHMARK.md](../docs/BENCHMARK.md#what-a-run-costs) for the cost
table.

## Options

| Flag | Default | Notes |
|---|---|---|
| `--model` | `claude-opus-5-5` | Recorded with the results. |
| `--effort` | `medium` | `low`–`max`. The depth control on current models. |
| `--temperature` | unset | **Current Claude models reject it with a 400.** Sampling parameters were removed; use `--effort`. Left in because older models still accept it; the harness raises a clear error if the model refuses. |
| `--runs` | `3` | Repeats of the whole suite. |
| `--dry-run` | off | Fixed placeholder SQL, no model. Exercises both arms and the auditor. Never a result. |

## Checking the harness before you spend anything

```bash
# The auditor must not share code with what it measures.
python -c "from benchmark.audit import verify_independence; verify_independence(); print('ok')"

# Both CHECK constraints must survive into the sandbox, or a catch gets
# credited to the wrong layer.
python -c "
from sqlalchemy import create_engine, text
from benchmark.seed import build
from benchmark import validators
from safeagentdb import ShadowDB
e = create_engine('sqlite:///' + str(build('benchmark/results/_probe.db')))
with ShadowDB(e, tables=['users','tasks'], tenant_id=42,
              tenant_column='tenant_id', reference_tables=['plans']) as sb:
    print('unsupported:', sb.unsupported_constraints)   # expect []
"

# What the agent is shown.
python -m benchmark.seed --print-schema
```

## Files

| File | Role |
|---|---|
| `seed.py` | Builds the stand-in production database. Deterministic. |
| `tasks.yaml` | The task suite and its ground-truth labels. Yours to write. |
| `agent.py` | One LLM agent, called once per task, returns SQL via tool call. |
| `run.py` | Runs both arms over the same SQL, audits each, writes raw JSON. |
| `audit.py` | Independent invariant checker. Imports neither SafeAgentDB nor the validators. |
| `validators.py` | The `SafeModel` schemas Arm B validates against. Kept apart from `audit.py` on purpose. |
| `report.py` | Aggregates runs, writes the summary, prints the table. |
