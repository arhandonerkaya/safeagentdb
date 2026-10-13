"""
reaudit.py -- score an old raw file with a newer auditor, if it reproduces.

A raw file keeps every statement the agent wrote and what each arm reported,
but not the databases the arms left behind. So an old run cannot simply be
re-audited: it has to be reconstructed. That is only trustworthy if the
reconstruction provably lands where the original did, so this script

1. re-runs every recorded statement through both arms, starting from the seed,
   with Arm B importing the SafeAgentDB checkout given as --library-root (the
   release the run used, e.g. a `git worktree` of 0.3.0 -- never the working
   tree);
2. scores both databases with auditor version 1 and compares every recorded
   violation and every recorded outcome field with what it reproduced;
3. only if all of it matches, scores the same databases with the current
   auditor and writes a NEW raw file. The original is never modified.

A file that does not reproduce is not scored at all, and the mismatches are
printed so the reason can be written down.

    git worktree add --detach ../sa-0.3.0 9417b12
    python -m benchmark.reaudit --raw benchmark/results/raw-<stamp>.json \\
        --library-root ../sa-0.3.0
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# Wall-clock figures are the only recorded fields allowed to differ.
_IGNORED = frozenset({"seconds"})


def _differences(recorded: Any, reproduced: Any, path: str = "") -> list[str]:
    if isinstance(recorded, dict) and isinstance(reproduced, dict):
        out = []
        for key in sorted(set(recorded) | set(reproduced)):
            if key in _IGNORED:
                continue
            if key not in recorded or key not in reproduced:
                out.append(f"{path}.{key}: present on one side only")
                continue
            out += _differences(recorded[key], reproduced[key], f"{path}.{key}")
        return out
    if recorded != reproduced:
        return [f"{path}: recorded {recorded!r}, reproduced {reproduced!r}"]
    return []


def _worker(raw_path: Path, library_root: Path, out_path: Path, report_path: Path) -> None:
    """Runs in its own interpreter, so the old library is the one imported."""
    # The library comes from the old checkout; the harness and the auditor from
    # this tree. The checkout is on sys.path only while safeagentdb is imported,
    # so a checkout that also contains benchmark/ cannot shadow the harness.
    sys.path.insert(0, str(library_root))
    try:
        import safeagentdb
    finally:
        sys.path.remove(str(library_root))

    imported = Path(safeagentdb.__file__).resolve()
    if library_root.resolve() not in imported.parents:
        raise SystemExit(
            f"safeagentdb was imported from {imported}, not from {library_root}. "
            f"Refusing to reconstruct a run with the wrong library."
        )

    from dataclasses import asdict

    from benchmark import audit as audit_module
    from benchmark.run import _row_counts, run_arm_a, run_arm_b
    from benchmark.seed import build, copy

    harness = Path(audit_module.__file__).resolve()
    if REPO_ROOT not in harness.parents:
        raise SystemExit(f"the harness was imported from {harness}, not from {REPO_ROOT}")

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    workdir = Path(tempfile.mkdtemp(prefix="reaudit-"))
    baseline = build(workdir / "seed.db")

    mismatches: list[str] = []
    rescored: list[dict[str, Any]] = []
    compared = 0
    for index, record in enumerate(raw["records"]):
        if record.get("arm_a") is None or record.get("arm_b") is None:
            rescored.append({**record, "auditor_version": audit_module.AUDITOR_VERSION})
            continue
        compared += 1
        statements = record["agent"]["statements"]
        tenant = int(record["tenant_id"])
        category = record["category"]

        db_a = copy(baseline, workdir / f"{index}-a.db")
        db_b = copy(baseline, workdir / f"{index}-b.db")
        arm_a = run_arm_a(db_a, statements)
        arm_b = run_arm_b(db_b, statements, tenant)

        reproduced = {}
        for arm, outcome, db in (("arm_a", arm_a, db_a), ("arm_b", arm_b, db_b)):
            reproduced[arm] = {
                **outcome,
                "violations": [
                    asdict(v) for v in audit_module.audit(db, baseline, tenant, category, version=1)
                ],
                "row_counts": _row_counts(db),
            }
            label = f"{record['task_id']} run {record['run']} {arm}"
            mismatches += [
                f"{label}{d}" for d in _differences(record[arm], reproduced[arm])
            ]

        new = {**record, "auditor_version": audit_module.AUDITOR_VERSION}
        for arm, db in (("arm_a", db_a), ("arm_b", db_b)):
            new[arm] = {
                **record[arm],
                "violations": [
                    asdict(v) for v in audit_module.audit(db, baseline, tenant, category)
                ],
            }
        rescored.append(new)

    library_commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, cwd=library_root,
    ).stdout.strip() or "unknown"
    report = {
        "source": str(raw_path),
        "records_with_arms": compared,
        "reproduced": not mismatches,
        "mismatches": mismatches,
        "library_root_commit": library_commit,
        "library_imported_from": str(imported.parent),
    }
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if mismatches:
        return  # not scored: nothing is written

    source_bytes = raw_path.read_bytes()
    out = {
        **{k: v for k, v in raw.items() if k != "records"},
        "auditor_version": audit_module.AUDITOR_VERSION,
        "reaudit": {
            "source": raw_path.as_posix(),
            "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
            "method": (
                "every recorded statement re-run through both arms from the seed, "
                "Arm B importing SafeAgentDB from the library commit below; auditor "
                "version 1 reproduced every recorded violation and outcome field "
                "(wall-clock seconds excepted) before the same databases were scored "
                "with the auditor version recorded here"
            ),
            "library_commit": library_commit,
            "records_with_arms": compared,
            "verified_against_auditor_version": 1,
        },
        "records": rescored,
    }
    out_path.write_text(json.dumps(out, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", required=True, nargs="+", help="original raw file(s)")
    parser.add_argument(
        "--library-root",
        required=True,
        help="a checkout of the SafeAgentDB release the run used, outside this tree",
    )
    parser.add_argument("--out-dir", default="benchmark/results")
    parser.add_argument("--worker", nargs=3, help=argparse.SUPPRESS)
    args = parser.parse_args()

    library_root = Path(args.library_root).resolve()
    if args.worker:
        raw_path, out_path, report_path = (Path(p) for p in args.worker)
        _worker(raw_path, library_root, out_path, report_path)
        return

    if library_root == REPO_ROOT or REPO_ROOT in library_root.parents:
        raise SystemExit("--library-root must be a separate checkout, not this tree")

    failed = False
    for raw in args.raw:
        raw_path = Path(raw)
        out_path = Path(args.out_dir) / f"reaudit-{raw_path.stem}-auditor2.json"
        if out_path.exists():
            raise SystemExit(f"{out_path} exists; refusing to overwrite a re-audit")
        report_path = Path(tempfile.mkdtemp(prefix="reaudit-report-")) / "report.json"
        subprocess.run(
            [sys.executable, "-m", "benchmark.reaudit", "--raw", str(raw_path),
             "--library-root", str(library_root),
             "--worker", str(raw_path), str(out_path), str(report_path)],
            check=True, cwd=REPO_ROOT,
        )
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if report["reproduced"]:
            print(
                f"{raw_path.name}: reproduced ({report['records_with_arms']} records with "
                f"arms, library {report['library_root_commit']} from "
                f"{report['library_imported_from']}) -> {out_path}"
            )
        else:
            failed = True
            print(
                f"{raw_path.name}: NOT reproduced, not scored. "
                f"{len(report['mismatches'])} mismatch(es):"
            )
            for line in report["mismatches"][:20]:
                print(f"    {line}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
