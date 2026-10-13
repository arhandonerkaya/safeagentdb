# benchmark/results/

Committed result files, so every published number can be recomputed with
`python -m benchmark.report --raw <file>` and re-adjudicated without re-running
a model. `work/` holds per-task database copies and is not committed.

| File | Condition | Model | SafeAgentDB | Runs | What it is |
|---|---|---|---|---|---|
| `raw-20261013T182422Z.json` | informed | `claude-haiku-4-5` | 0.2.0 (stale install; the code was 0.3.0) | 1 | The failed run: all 68 calls rejected with HTTP 400 for sending `effort`. Kept as the evidence behind the harness's agent-error handling. Not a result. |
| `raw-20261013T183342Z.json` | informed | `claude-haiku-4-5` | 0.3.0 | 1 | The first real run. The file records 21 agent errors; they were prose answers, and the report now reads them as declines (25 in all). The evidence for B-1 in `docs/REVIEW_0_3.md`. |
| `raw-20261013T190701Z.json` | informed | `claude-sonnet-5-5`, effort `medium` | 0.3.0 | 3 | **Pre-B-1 baseline.** Three safe inserts blocked in every run by B-1, fixed in 0.3.1. |
| `20261013T191835Z.json` | informed | `claude-sonnet-5-5`, effort `medium` | 0.3.0 | 3 | The summary of the baseline above, as `report.py` wrote it then (summary schema 1). |

Files written before `--condition` existed carry no condition field and read as
`informed`, the only prompt there was. Their recorded commits (`0f56a21`,
`a59ce4a`, `e9e191b`) were rewritten when the benchmark branch was rebased onto
0.3.1; the library code in each is the 0.3.0 release plus at most the
`__version__` line. See [Results](../../docs/BENCHMARK.md#results).
