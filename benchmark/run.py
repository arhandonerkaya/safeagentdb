"""
run.py -- execute the task suite through both arms.

For each task the agent is asked once for SQL. Those same statements are then
run twice, each against its own fresh copy of the seeded database:

    Arm A (control)  straight at the database, the way an agent with a DB
                     connection and no safety layer would. The database still
                     enforces its own constraints -- this arm is "no
                     SafeAgentDB", not "no protection at all".

    Arm B            through ShadowDB: the statements run in the sandbox, the
                     diff is taken, and commit_to_production() decides.

After each arm the independent auditor compares that arm's database against
the seed and reports any broken invariant.

    export ANTHROPIC_API_KEY=...
    python -m benchmark.run --runs 3 --model claude-opus-5-5
    python -m benchmark.run --dry-run          # no API calls; wiring check only

--condition picks what the agent is told, or replaces it:

    informed     (default) the multi-tenant system prompt
    uninformed   a plain database assistant: no tenancy rules, no hints
    replay       no model call: each task's fixed replay_sql from tasks.yaml.
                 Free and deterministic, so --runs 1 is enough.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import warnings
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import create_engine

from benchmark import audit as audit_module
from benchmark import validators  # noqa: F401 -- importing registers the SafeModels
from benchmark.agent import (
    API_REFUSAL,
    CONDITIONS,
    EMPTY_STATEMENTS,
    INFORMED,
    PROMPT_VERSION,
    PROMPT_VERSIONS,
    REPLAY,
    AgentConfig,
    AgentResult,
    EffortNotSupported,
    TemperatureNotSupported,
    assert_no_credential,
    build_client,
    generate_sql,
    redact,
)
from benchmark.seed import TENANT_A, build, copy
from safeagentdb import SafeAgentDBError, ShadowDB

TENANT_COLUMN = "tenant_id"
SANDBOX_TABLES = ["users", "tasks"]
REFERENCE_TABLES = ["plans"]

REQUIRED_FIELDS = ("id", "prompt", "tenant_id", "category", "expected", "expected_effect")
VALID_EXPECTED = ("safe", "unsafe")

# Identical failures at the start of a run mean the rest will fail identically.
FAIL_FAST_AFTER = 3


# ---------------------------------------------------------------------------
# Task loading
# ---------------------------------------------------------------------------


def load_tasks(path: str | Path, *, require_replay: bool = False) -> list[dict[str, Any]]:
    """Read and validate the task suite, so a typo fails before any API spend.

    ``replay_sql`` is checked wherever it appears, and required on every task
    when the replay condition is going to run it.
    """
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    tasks = document.get("tasks") or []
    if not tasks:
        raise SystemExit(f"{path} defines no tasks")

    seen: set[str] = set()
    for index, task in enumerate(tasks):
        missing = [f for f in REQUIRED_FIELDS if f not in task]
        if missing:
            raise SystemExit(f"task #{index} is missing {missing}")
        if task["expected"] not in VALID_EXPECTED:
            raise SystemExit(
                f"task {task['id']!r}: expected must be one of {VALID_EXPECTED}, "
                f"got {task['expected']!r}"
            )
        if task["id"] in seen:
            raise SystemExit(f"duplicate task id {task['id']!r}")
        seen.add(task["id"])

        replay = task.get("replay_sql")
        if replay is None:
            if require_replay:
                raise SystemExit(f"task {task['id']!r} has no replay_sql to replay")
        elif (
            not isinstance(replay, list)
            or not replay
            or not all(isinstance(s, str) and s.strip() for s in replay)
        ):
            raise SystemExit(
                f"task {task['id']!r}: replay_sql must be a non-empty list of SQL "
                f"statements, one per entry"
            )

    return tasks


# ---------------------------------------------------------------------------
# Arm A -- straight at the database
# ---------------------------------------------------------------------------


def run_arm_a(db_path: Path, statements: list[str]) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "applied": 0,
        "error": None,
        "error_type": None,
        "seconds": 0.0,
    }
    if not statements:
        return outcome

    started = time.perf_counter()
    conn = sqlite3.connect(db_path)
    try:
        # A real production database enforces its foreign keys; SQLite needs
        # asking. Leaving this off would hand Arm A a disadvantage the control
        # is not supposed to have.
        conn.execute("PRAGMA foreign_keys=ON")
        for statement in statements:
            conn.execute(statement)
            outcome["applied"] += 1
        conn.commit()
    except Exception as exc:  # noqa: BLE001 -- the control arm records anything
        conn.rollback()
        outcome["error_type"] = type(exc).__name__
        outcome["error"] = redact(str(exc))
    finally:
        conn.close()
        outcome["seconds"] = round(time.perf_counter() - started, 3)

    return outcome


# ---------------------------------------------------------------------------
# Arm B -- through SafeAgentDB
# ---------------------------------------------------------------------------


def run_arm_b(db_path: Path, statements: list[str], tenant_id: int) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "rows_written": 0,
        "blocked": False,
        "blocked_at": None,
        "error": None,
        "error_type": None,
        "diff_valid": None,
        "diff_summary": None,
        "unsupported_constraints": [],
        "skipped_conflicts": [],
        "seconds": 0.0,
    }
    if not statements:
        return outcome

    engine = create_engine(f"sqlite:///{db_path}")
    started = time.perf_counter()
    try:
        with ShadowDB(
            engine,
            tables=SANDBOX_TABLES,
            tenant_id=tenant_id,
            tenant_column=TENANT_COLUMN,
            reference_tables=REFERENCE_TABLES,
        ) as sandbox:
            outcome["unsupported_constraints"] = sandbox.unsupported_constraints

            stage = "sandbox"
            try:
                for statement in statements:
                    sandbox.execute(statement)
                sandbox.session.commit()

                stage = "diff"
                changeset = sandbox.diff()
                outcome["diff_valid"] = changeset.is_valid
                outcome["diff_summary"] = changeset.summary

                stage = "commit"
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    outcome["rows_written"] = sandbox.commit_to_production()
                outcome["skipped_conflicts"] = [
                    asdict(s) for s in sandbox.skipped_conflicts
                ]
            except Exception as exc:  # noqa: BLE001 -- classify, do not swallow
                outcome["blocked"] = True
                outcome["blocked_at"] = stage
                outcome["error_type"] = type(exc).__name__
                outcome["error"] = redact(str(exc)[:600])
                outcome["safeagentdb_error"] = isinstance(exc, SafeAgentDBError)
    except Exception as exc:  # noqa: BLE001 -- a refused sandbox is also a block
        outcome["blocked"] = True
        outcome["blocked_at"] = "open"
        outcome["error_type"] = type(exc).__name__
        outcome["error"] = redact(str(exc)[:600])
        outcome["safeagentdb_error"] = isinstance(exc, SafeAgentDBError)
    finally:
        engine.dispose()
        outcome["seconds"] = round(time.perf_counter() - started, 3)

    return outcome


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _row_counts(db_path: Path) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("plans", "users", "tasks")
        }
    finally:
        conn.close()


class AgentFailing(RuntimeError):
    """Raised to stop a run whose opening agent calls all fail the same way.

    The first real run spent 68 calls on an error the first one already showed.
    """


def _agent_error(agent: AgentResult) -> str | None:
    """The call itself failed: API or transport. Nothing to measure, and
    nothing about the model to read from it."""
    return agent.error or None


def _agent_decline(agent: AgentResult) -> str | None:
    """The call worked and the model chose not to write SQL.

    An outcome, not a failure: for an unsafe request it is the correct one. It
    still leaves nothing for the two arms to execute, so the arms are skipped
    and the record carries the model's own words instead.

    Decided on the shape of the result rather than on the marker alone, so that
    an empty statement list can never reach the arms and be reported as a task
    that ran cleanly.
    """
    if agent.declined:
        return agent.declined
    if agent.refusal:
        return API_REFUSAL
    if not agent.statements:
        return EMPTY_STATEMENTS
    return None


def run_once(
    tasks: list[dict[str, Any]],
    config: AgentConfig,
    workdir: Path,
    *,
    dry_run: bool,
    run_index: int,
) -> list[dict[str, Any]]:
    baseline = build(workdir / "seed.db")
    # Replay never talks to a model, so it never builds a client: no
    # credential is read, and nothing can be billed.
    client = None if dry_run or not config.calls_model else build_client()
    version = library_version_number()
    records: list[dict[str, Any]] = []
    opening_failures: list[str] = []
    # Set once a task has actually been through the two arms, which proves the
    # run can produce a measurement and turns the fail-fast guard off.
    measured_any = False

    for task in tasks:
        tenant_id = int(task.get("tenant_id", TENANT_A))

        if dry_run:
            agent = AgentResult(
                statements=[f"UPDATE tasks SET status='done' WHERE tenant_id={tenant_id}"],
                rationale="dry run: fixed placeholder SQL, no API call made",
            )
        elif not config.calls_model:
            agent = AgentResult(
                statements=[s.strip().rstrip(";") for s in task["replay_sql"]],
                rationale="replay: the task's fixed replay_sql, no model involved",
            )
        else:
            agent = generate_sql(client, config, task["prompt"], tenant_id)

        error = _agent_error(agent)
        declined = None if error else _agent_decline(agent)
        status = "unsafe" if task["expected"] == "unsafe" else "safe "

        if error is not None or declined is not None:
            # No SQL either way, so there is nothing for the arms to execute.
            # Running them anyway produced "A:0v B:0v applied" -- indistinguishable
            # from a clean task, which is how 68 failed calls looked like a
            # successful run.
            records.append(
                {
                    "run": run_index,
                    "condition": config.condition,
                    "safeagentdb_version": version,
                    "auditor_version": audit_module.AUDITOR_VERSION,
                    "prompt_version": config.prompt_version,
                    "task_id": task["id"],
                    "category": task["category"],
                    "expected": task["expected"],
                    "expected_effect": task["expected_effect"],
                    "tenant_id": tenant_id,
                    "agent_error": error,
                    "agent_declined": declined,
                    "agent": agent.as_dict(),
                    "arm_a": None,
                    "arm_b": None,
                }
            )
            if error is not None:
                print(
                    f"  run {run_index} {task['id']:<28} {status} "
                    f"AGENT ERROR: {_short(error)}"
                )
            else:
                print(
                    f"  run {run_index} {task['id']:<28} {status} "
                    f"DECLINED ({declined}): {_short(agent.answer, 60)}"
                )

            # Fail fast: the same failure every time, with nothing having worked
            # yet, means the rest of the suite will fail the same way and cost
            # the same money. Declines are not counted but do not clear the
            # streak either: a model that declines still says nothing about
            # whether the next call will be accepted.
            if error is not None and not measured_any:
                opening_failures.append(error)
                if len(opening_failures) >= FAIL_FAST_AFTER and len(
                    set(opening_failures)
                ) == 1:
                    raise AgentFailing(
                        f"{FAIL_FAST_AFTER} agent calls failed with the same "
                        f"error and nothing has run yet, so the remaining "
                        f"{len(tasks) - len(records)} would too. Stopping before "
                        f"they are paid for.\n\n  {error}"
                    )
            continue

        db_a = copy(baseline, workdir / f"run{run_index}-{task['id']}-a.db")
        db_b = copy(baseline, workdir / f"run{run_index}-{task['id']}-b.db")

        arm_a = run_arm_a(db_a, agent.statements)
        arm_b = run_arm_b(db_b, agent.statements, tenant_id)

        # The task's label is the only thing the auditor is told about the task.
        violations_a = audit_module.audit(db_a, baseline, tenant_id, task["category"])
        violations_b = audit_module.audit(db_b, baseline, tenant_id, task["category"])

        records.append(
            {
                "run": run_index,
                "condition": config.condition,
                "safeagentdb_version": version,
                "auditor_version": audit_module.AUDITOR_VERSION,
                "prompt_version": config.prompt_version,
                "task_id": task["id"],
                "category": task["category"],
                "expected": task["expected"],
                "expected_effect": task["expected_effect"],
                "tenant_id": tenant_id,
                "agent_error": None,
                "agent_declined": None,
                "agent": agent.as_dict(),
                "arm_a": {
                    **arm_a,
                    "violations": [asdict(v) for v in violations_a],
                    "row_counts": _row_counts(db_a),
                },
                "arm_b": {
                    **arm_b,
                    "violations": [asdict(v) for v in violations_b],
                    "row_counts": _row_counts(db_b),
                },
            }
        )

        measured_any = True

        print(
            f"  run {run_index} {task['id']:<28} {status} "
            f"A:{len(violations_a)}v {'ERR' if arm_a['error'] else '   '} "
            f"B:{len(violations_b)}v {'BLOCKED' if arm_b['blocked'] else 'applied'}"
        )

    return records


def _short(message: str, limit: int = 90) -> str:
    """One line, trimmed, so a console row stays readable."""
    flat = " ".join(str(message).split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default="benchmark/tasks.yaml")
    parser.add_argument(
        "--condition",
        choices=CONDITIONS,
        default=INFORMED,
        help=(
            "informed (default): the multi-tenant system prompt. uninformed: a "
            "plain database assistant with no tenancy rules or hints. replay: no "
            "model call -- each task's fixed replay_sql runs in both arms, at no "
            "cost and deterministically, so --runs 1 is enough."
        ),
    )
    parser.add_argument("--runs", type=int, default=3, help="repeats of the whole suite")
    parser.add_argument("--model", default=AgentConfig.model)
    parser.add_argument(
        "--effort",
        default=None,
        help=(
            "low | medium | high | xhigh | max, or 'none' to omit the parameter. "
            "Omitted entirely unless asked for: not every model accepts it "
            "(Haiku 4.5 rejects it with a 400). Recorded with the results."
        ),
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=None,
        help=(
            "only for models that still accept it; the current Claude models "
            "reject it with a 400 -- use --effort instead"
        ),
    )
    parser.add_argument("--max-tokens", type=int, default=AgentConfig.max_tokens)
    parser.add_argument(
        "--prompt-version",
        type=int,
        choices=PROMPT_VERSIONS,
        default=PROMPT_VERSION,
        help=(
            "2 (default): the schema shows only the acting tenant's rows. 1: every "
            "tenant's rows, as in the runs before 2026-10-13. Recorded with the results."
        ),
    )
    parser.add_argument(
        "--estimate",
        action="store_true",
        help=(
            "print the expected API cost for this condition, model, effort and --runs, "
            "from the token usage recorded in benchmark/results, and exit. No call is made."
        ),
    )
    parser.add_argument(
        "--workdir",
        default="benchmark/results/work",
        help="scratch space for the per-task database copies",
    )
    parser.add_argument("--out", default=None, help="where to write the raw results")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="exercise the harness with fixed SQL and no API calls",
    )
    args = parser.parse_args()
    if args.dry_run and args.condition == REPLAY:
        parser.error("--dry-run and --condition replay both skip the model; pick one")

    # Before spending anything: the auditor must still be independent, and the
    # task file must parse.
    audit_module.verify_independence()
    tasks = load_tasks(args.tasks, require_replay=args.condition == REPLAY)

    if args.estimate:
        from benchmark.estimate import estimate, render

        result = estimate(args.condition, args.model, args.effort, args.runs, len(tasks))
        print(render(result, args.condition, args.model, args.effort, args.runs))
        return

    config = AgentConfig(
        model=args.model,
        effort=args.effort,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
        condition=args.condition,
        prompt_version=args.prompt_version,
    )

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc)
    if args.dry_run:
        unit = "dry-run iterations"
    elif not config.calls_model:
        unit = "replayed scripts, no API calls"
    else:
        unit = "API calls"
    print(
        f"condition: {args.condition}\n"
        f"{len(tasks)} tasks x {args.runs} runs = {len(tasks) * args.runs} {unit}"
    )

    records: list[dict[str, Any]] = []
    stopped_early: str | None = None
    try:
        for run_index in range(1, args.runs + 1):
            records += run_once(
                tasks, config, workdir, dry_run=args.dry_run, run_index=run_index
            )
    except (AgentFailing, TemperatureNotSupported, EffortNotSupported) as exc:
        stopped_early = str(exc)
        print(f"\nSTOPPED: {exc}")

    finished = datetime.now(timezone.utc)
    raw = {
        # 2 added agent_declined per record and agent_declines per file.
        # 3 added the condition, and the bare version number, per file and record.
        # 4 added the auditor version, per file and record.
        # 5 added the prompt version, per file and record.
        "schema_version": 5,
        "condition": config.condition,
        "auditor_version": audit_module.AUDITOR_VERSION,
        "prompt_version": config.prompt_version,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "dry_run": args.dry_run,
        "runs": args.runs,
        "task_file": str(args.tasks),
        "task_count": len(tasks),
        "agent_config": config.as_dict(),
        "stopped_early": stopped_early,
        "agent_errors": sum(1 for r in records if r.get("agent_error")),
        "agent_declines": sum(1 for r in records if r.get("agent_declined")),
        "safeagentdb_version": _library_version(),
        "safeagentdb_version_number": library_version_number(),
        "safeagentdb_commit": _library_commit(),
        "records": records,
    }

    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out or f"benchmark/results/raw-{stamp}.json")
    out.parent.mkdir(parents=True, exist_ok=True)

    serialized = json.dumps(raw, indent=2)
    assert_no_credential(serialized)
    out.write_text(serialized, encoding="utf-8")

    failed = raw["agent_errors"]
    if failed:
        print(
            f"\n{failed} of {len(records)} agent call(s) failed and were "
            f"recorded as agent_error. They are excluded from every metric."
        )

    declines = raw["agent_declines"]
    if declines:
        print(
            f"\n{declines} of {len(records)} agent call(s) declined to write SQL. "
            f"That is an outcome, not a failure, and the report counts it as one. "
            f"Each record keeps the model's answer under agent.answer."
        )

    print(f"\nraw results -> {out}")
    print(f"now run:  python -m benchmark.report --raw {out}")
    if args.condition == REPLAY and args.runs > 1:
        print("(replay is deterministic: the extra runs repeated identical work)")

    if stopped_early or (records and failed == len(records)):
        raise SystemExit(1)


def library_version_number() -> str:
    """The bare version of the imported safeagentdb, e.g. '0.3.1'.

    What the report groups on, so results from two versions are never pooled.
    """
    import safeagentdb

    return str(getattr(safeagentdb, "__version__", "unknown"))


def _library_version() -> str:
    """The version of the safeagentdb that was actually imported.

    importlib.metadata reads the *installed distribution*, which a stale
    editable install makes a lie: the first real run recorded 0.2.0 while the
    0.3.0 working tree was the code under test. safeagentdb.__version__ lives in
    the package that got imported, so it cannot drift from it. The installed
    metadata is still reported when it disagrees, because a mismatch is worth
    seeing.
    """
    import safeagentdb

    actual = getattr(safeagentdb, "__version__", "unknown")
    location = Path(safeagentdb.__file__).parent

    try:
        from importlib.metadata import version

        installed = version("safeagentdb")
    except Exception:  # noqa: BLE001 -- not installed at all
        installed = None

    if installed and installed != actual:
        return (
            f"{actual} from {location} "
            f"(installed distribution metadata says {installed}; stale)"
        )
    return f"{actual} from {location}"


def _library_commit() -> str:
    """The working tree's HEAD, so a published result pins the exact code."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parent.parent,
        )
        head = result.stdout.strip() if result.returncode == 0 else "unknown"
    except Exception:  # noqa: BLE001 -- git absent or not a checkout
        return "unknown"

    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=Path(__file__).resolve().parent.parent,
        )
        if dirty.returncode == 0 and dirty.stdout.strip():
            head += "-dirty"
    except Exception:  # noqa: BLE001
        pass
    return head


if __name__ == "__main__":
    main()
