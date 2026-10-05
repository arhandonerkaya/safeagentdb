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
from benchmark.agent import AgentConfig, AgentResult, build_client, generate_sql
from benchmark.seed import TENANT_A, build, copy
from safeagentdb import SafeAgentDBError, ShadowDB

TENANT_COLUMN = "tenant_id"
SANDBOX_TABLES = ["users", "tasks"]
REFERENCE_TABLES = ["plans"]

REQUIRED_FIELDS = ("id", "prompt", "tenant_id", "category", "expected", "expected_effect")
VALID_EXPECTED = ("safe", "unsafe")


# ---------------------------------------------------------------------------
# Task loading
# ---------------------------------------------------------------------------


def load_tasks(path: str | Path) -> list[dict[str, Any]]:
    """Read and validate the task suite, so a typo fails before any API spend."""
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
        outcome["error"] = str(exc)
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
                outcome["error"] = str(exc)[:600]
                outcome["safeagentdb_error"] = isinstance(exc, SafeAgentDBError)
    except Exception as exc:  # noqa: BLE001 -- a refused sandbox is also a block
        outcome["blocked"] = True
        outcome["blocked_at"] = "open"
        outcome["error_type"] = type(exc).__name__
        outcome["error"] = str(exc)[:600]
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


def run_once(
    tasks: list[dict[str, Any]],
    config: AgentConfig,
    workdir: Path,
    *,
    dry_run: bool,
    run_index: int,
) -> list[dict[str, Any]]:
    baseline = build(workdir / "seed.db")
    client = None if dry_run else build_client()
    records: list[dict[str, Any]] = []

    for task in tasks:
        tenant_id = int(task.get("tenant_id", TENANT_A))

        if dry_run:
            agent = AgentResult(
                statements=[f"UPDATE tasks SET status='done' WHERE tenant_id={tenant_id}"],
                rationale="dry run: fixed placeholder SQL, no API call made",
            )
        else:
            agent = generate_sql(client, config, task["prompt"], tenant_id)

        db_a = copy(baseline, workdir / f"run{run_index}-{task['id']}-a.db")
        db_b = copy(baseline, workdir / f"run{run_index}-{task['id']}-b.db")

        arm_a = run_arm_a(db_a, agent.statements)
        arm_b = run_arm_b(db_b, agent.statements, tenant_id)

        violations_a = audit_module.audit(db_a, baseline, tenant_id)
        violations_b = audit_module.audit(db_b, baseline, tenant_id)

        records.append(
            {
                "run": run_index,
                "task_id": task["id"],
                "category": task["category"],
                "expected": task["expected"],
                "expected_effect": task["expected_effect"],
                "tenant_id": tenant_id,
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

        status = "unsafe" if task["expected"] == "unsafe" else "safe "
        print(
            f"  run {run_index} {task['id']:<28} {status} "
            f"A:{len(violations_a)}v {'ERR' if arm_a['error'] else '   '} "
            f"B:{len(violations_b)}v {'BLOCKED' if arm_b['blocked'] else 'applied'}"
        )

    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default="benchmark/tasks.yaml")
    parser.add_argument("--runs", type=int, default=3, help="repeats of the whole suite")
    parser.add_argument("--model", default=AgentConfig.model)
    parser.add_argument(
        "--effort",
        default=AgentConfig.effort,
        help="low | medium | high | xhigh | max (recorded with the results)",
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

    # Before spending anything: the auditor must still be independent, and the
    # task file must parse.
    audit_module.verify_independence()
    tasks = load_tasks(args.tasks)

    config = AgentConfig(
        model=args.model,
        effort=args.effort,
        temperature=args.temperature,
        max_tokens=args.max_tokens,
    )

    workdir = Path(args.workdir)
    workdir.mkdir(parents=True, exist_ok=True)

    started = datetime.now(timezone.utc)
    print(
        f"{len(tasks)} tasks x {args.runs} runs = {len(tasks) * args.runs} "
        f"{'dry-run iterations' if args.dry_run else 'API calls'}"
    )

    records: list[dict[str, Any]] = []
    for run_index in range(1, args.runs + 1):
        records += run_once(
            tasks, config, workdir, dry_run=args.dry_run, run_index=run_index
        )

    finished = datetime.now(timezone.utc)
    raw = {
        "schema_version": 1,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "dry_run": args.dry_run,
        "runs": args.runs,
        "task_file": str(args.tasks),
        "task_count": len(tasks),
        "agent_config": config.as_dict(),
        "safeagentdb_version": _library_version(),
        "safeagentdb_commit": _library_commit(),
        "records": records,
    }

    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out or f"benchmark/results/raw-{stamp}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(raw, indent=2), encoding="utf-8")

    print(f"\nraw results -> {out}")
    print(f"now run:  python -m benchmark.report --raw {out}")


def _library_version() -> str:
    """The version of the safeagentdb that is actually imported.

    Read from the package rather than from pyproject.toml: a pip-installed copy
    shadowing the working tree is exactly the mix-up this is here to surface.
    """
    import safeagentdb

    try:
        from importlib.metadata import version

        installed = version("safeagentdb")
    except Exception:  # noqa: BLE001 -- editable install without metadata
        installed = "unknown"
    return f"{installed} from {Path(safeagentdb.__file__).parent}"


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
