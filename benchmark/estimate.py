"""
estimate.py -- what a run would cost, from what earlier runs actually used.

No API call, no client, no key. Token usage per call is read from the raw
files in benchmark/results/, the closest recorded match to the requested
condition, model and effort is used as the basis, and the call count is
tasks x runs. The basis it chose is printed with the figure, so a guess built
on another model's or condition's usage cannot pass for a measurement.

    python -m benchmark.run --estimate --condition uninformed --model claude-sonnet-5-5 --runs 3
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# USD per million tokens. From the Claude API reference's model table (cached
# 2026-10-06): input and output as listed; cache reads as listed for Opus 5.5
# and Sonnet 5.5, 0.1x input elsewhere; 5-minute cache writes 1.25x input,
# which is the TTL agent.py asks for. Check current pricing before quoting a
# figure: these go stale.
PRICES: dict[str, dict[str, float]] = {
    "claude-opus-5-5": {"input": 4.00, "output": 20.00, "cache_write": 5.00, "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.20},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_write": 1.25, "cache_read": 0.10},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01},
}

_USAGE_TO_PRICE = {
    "input_tokens": "input",
    "output_tokens": "output",
    "cache_creation_input_tokens": "cache_write",
    "cache_read_input_tokens": "cache_read",
}


@dataclass
class Estimate:
    calls: int
    mean_cost: float
    p90_cost: float
    per_call_tokens: dict[str, float]
    basis: str
    sample_calls: int


def _recorded_calls(results_dir: Path) -> list[dict[str, Any]]:
    """Every agent call with recorded usage: (condition, model, effort, usage)."""
    from benchmark.report import condition_of

    calls = []
    for path in sorted(results_dir.glob("raw-*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        config = raw.get("agent_config") or {}
        model = config.get("model")
        effort = config.get("effort_sent", config.get("effort"))
        for record in raw.get("records") or []:
            usage = (record.get("agent") or {}).get("usage") or {}
            if not usage or not model:
                continue  # replay, dry run, or a call that failed before usage
            calls.append(
                {
                    "condition": condition_of(raw, record),
                    "model": model,
                    "effort": effort,
                    "usage": usage,
                    "source": path.name,
                }
            )
    return calls


def _cost(usage: dict[str, int], prices: dict[str, float]) -> float:
    return sum(usage.get(k, 0) * prices[p] for k, p in _USAGE_TO_PRICE.items()) / 1_000_000


def estimate(
    condition: str,
    model: str,
    effort: str | None,
    runs: int,
    tasks: int,
    results_dir: Path = Path("benchmark/results"),
) -> Estimate:
    calls = tasks * runs
    # --effort none and no --effort both send nothing, which is what is recorded.
    effort = None if effort in (None, "none") else effort
    if condition == "replay":
        return Estimate(calls, 0.0, 0.0, {}, "replay calls no model", 0)
    if model not in PRICES:
        raise SystemExit(
            f"no price for {model!r} in benchmark/estimate.py; add it from the "
            f"current pricing page before estimating"
        )

    recorded = _recorded_calls(results_dir)
    tiers = [
        ("same condition, model and effort",
         lambda c: (c["condition"], c["model"], c["effort"]) == (condition, model, effort)),
        ("same condition and model, other effort",
         lambda c: (c["condition"], c["model"]) == (condition, model)),
        ("same model, informed condition",
         lambda c: c["model"] == model),
        ("another model's token usage, priced as " + model,
         lambda c: True),
    ]
    matched = next(
        ((label, sample) for label, keep in tiers if (sample := [c for c in recorded if keep(c)])),
        None,
    )
    if matched is None:
        raise SystemExit("no recorded run has token usage to base an estimate on")
    label, sample = matched

    prices = PRICES[model]
    costs = sorted(_cost(c["usage"], prices) for c in sample)
    p90 = costs[min(len(costs) - 1, int(round(0.9 * (len(costs) - 1))))]
    per_call = {
        key: statistics.fmean(c["usage"].get(key, 0) for c in sample) for key in _USAGE_TO_PRICE
    }
    sources = sorted({c["source"] for c in sample})
    basis = f"{label}: {len(sample)} recorded calls from {', '.join(sources)}"
    return Estimate(
        calls, statistics.fmean(costs) * calls, p90 * calls, per_call, basis, len(sample)
    )


def render(result: Estimate, condition: str, model: str, effort: str | None, runs: int) -> str:
    if condition == "replay":
        return f"replay: {result.calls} scripts, no API calls, $0.00"
    tokens = ", ".join(f"{k.replace('_input_tokens', '').replace('_tokens', '')} {v:,.0f}"
                       for k, v in result.per_call_tokens.items())
    return "\n".join(
        [
            f"estimate for --condition {condition} --model {model} "
            f"--effort {effort or '(not sent)'} --runs {runs}",
            f"  calls:    {result.calls}",
            f"  expected: ${result.mean_cost:,.2f}   (mean recorded cost per call x calls)",
            f"  high:     ${result.p90_cost:,.2f}   (90th-percentile call x calls)",
            f"  per call: {tokens} tokens",
            f"  basis:    {result.basis}",
            "  No call was made. Prices are from benchmark/estimate.py; check them first.",
        ]
    )
