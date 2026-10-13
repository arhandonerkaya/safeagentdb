"""
test_benchmark_harness.py -- the benchmark harness must not report a failure
as a result.

The first real run spent 68 API calls, every one of which returned HTTP 400
because `run.py` sent `effort=medium` to a model that rejects it, and the
console printed `A:0v B:0v applied` for all 68 -- byte-identical to a clean
dry run. These tests drive the harness with a fake agent that fails, so that
can never read as success again.

Plain pytest, no API calls, file-based SQLite in tmp_path.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("yaml", reason="the benchmark harness needs pyyaml")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmark import report as report_module  # noqa: E402
from benchmark import run as run_module  # noqa: E402
from benchmark.agent import OMIT_EFFORT, AgentConfig, AgentResult  # noqa: E402

TASKS = [
    {
        "id": f"t-{index:02d}",
        "prompt": "do the thing",
        "tenant_id": 42,
        "category": "legitimate",
        "expected": "safe",
        "expected_effect": "something",
    }
    for index in range(1, 7)
]


@pytest.fixture
def workdir(tmp_path):
    path = tmp_path / "work"
    path.mkdir()
    return path


def _always_fails(message="BadRequestError: 400 - effort not supported"):
    """A fake agent that fails the same way every call."""
    calls = []

    def fake(client, config, prompt, tenant_id):
        calls.append(prompt)
        return AgentResult(error=message, seconds=0.01)

    fake.calls = calls
    return fake


def _fails_then_works(failures: int):
    """Fails `failures` times with distinct errors, then returns usable SQL."""
    calls = []

    def fake(client, config, prompt, tenant_id):
        calls.append(prompt)
        index = len(calls)
        if index <= failures:
            return AgentResult(error=f"transient error #{index}", seconds=0.01)
        return AgentResult(
            statements=[f"UPDATE tasks SET status='done' WHERE id=1 AND tenant_id={tenant_id}"],
            rationale="ok",
            seconds=0.01,
        )

    fake.calls = calls
    return fake


@pytest.fixture
def no_client(monkeypatch):
    monkeypatch.setattr(run_module, "build_client", lambda: object())


# ============================================================
# 1. The effort parameter is only sent when asked for
# ============================================================


class TestEffortIsOptional:
    def test_it_is_not_sent_by_default(self):
        config = AgentConfig(model="claude-haiku-4-5")
        assert config.effort is None
        assert config.sends_effort is False
        assert config.as_dict()["effort_sent"] is None

    def test_none_omits_it_explicitly(self):
        config = AgentConfig(model="claude-haiku-4-5", effort=OMIT_EFFORT)
        assert config.sends_effort is False
        recorded = config.as_dict()
        assert recorded["effort_requested"] == OMIT_EFFORT
        assert recorded["effort_sent"] is None

    def test_a_real_level_is_sent_and_recorded(self):
        config = AgentConfig(model="claude-opus-5-5", effort="high")
        assert config.sends_effort is True
        recorded = config.as_dict()
        assert recorded["effort_requested"] == "high"
        assert recorded["effort_sent"] == "high"

    def test_the_request_omits_output_config_when_not_asked_for(self, monkeypatch):
        """What actually goes on the wire, which is what the 400 was about."""
        captured = {}

        class FakeMessages:
            def create(self, **kwargs):
                captured.update(kwargs)
                raise RuntimeError("stop here; the request is what matters")

        class FakeClient:
            messages = FakeMessages()

        from benchmark import agent as agent_module

        with pytest.raises(RuntimeError):
            agent_module.generate_sql(
                FakeClient(), AgentConfig(model="claude-haiku-4-5"), "p", 42
            )
        assert "output_config" not in captured

        captured.clear()
        with pytest.raises(RuntimeError):
            agent_module.generate_sql(
                FakeClient(),
                AgentConfig(model="claude-opus-5-5", effort="low"),
                "p",
                42,
            )
        assert captured["output_config"] == {"effort": "low"}


# ============================================================
# 2. An agent failure never looks like a result
# ============================================================


class TestAgentErrorsAreVisible:
    def test_a_failed_call_is_marked_and_both_arms_are_skipped(
        self, workdir, no_client, monkeypatch, capsys
    ):
        monkeypatch.setattr(run_module, "generate_sql", _fails_then_works(1))

        records = run_module.run_once(
            TASKS[:2], AgentConfig(), workdir, dry_run=False, run_index=1
        )

        failed, worked = records
        assert failed["agent_error"]
        assert failed["arm_a"] is None
        assert failed["arm_b"] is None
        # The successful one still measures both arms.
        assert worked["agent_error"] is None
        assert worked["arm_a"] is not None
        assert worked["arm_b"] is not None

    def test_the_console_says_agent_error(self, workdir, no_client, monkeypatch, capsys):
        """The whole point: the old output was 'A:0v B:0v applied'."""
        monkeypatch.setattr(run_module, "generate_sql", _fails_then_works(1))

        run_module.run_once(
            TASKS[:2], AgentConfig(), workdir, dry_run=False, run_index=1
        )
        out = capsys.readouterr().out

        assert "AGENT ERROR" in out
        assert "transient error #1" in out
        failed_line = next(line for line in out.splitlines() if "t-01" in line)
        assert "A:0v" not in failed_line
        assert "applied" not in failed_line

    def test_a_refusal_counts_as_a_failure_too(self, workdir, no_client, monkeypatch):
        def refuses(client, config, prompt, tenant_id):
            return AgentResult(refusal="cyber", seconds=0.01)

        monkeypatch.setattr(run_module, "generate_sql", refuses)

        (record,) = run_module.run_once(
            TASKS[:1], AgentConfig(), workdir, dry_run=False, run_index=1
        )
        assert "refused" in record["agent_error"]
        assert record["arm_a"] is None


# ============================================================
# 3. The report excludes them, counts them, and warns
# ============================================================


class TestReportExcludesAgentErrors:
    @staticmethod
    def _raw(records):
        return {
            "schema_version": 1,
            "dry_run": False,
            "runs": 1,
            "task_count": len(records),
            "agent_config": AgentConfig().as_dict(),
            "safeagentdb_version": "0.3.0",
            "records": records,
        }

    @staticmethod
    def _good(task_id, violations_a=0):
        return {
            "run": 1,
            "task_id": task_id,
            "category": "legitimate",
            "expected": "safe",
            "expected_effect": "x",
            "tenant_id": 42,
            "agent_error": None,
            "agent": {
                "statements": ["UPDATE tasks SET status='done'"],
                "seconds": 0.1,
                "usage": {},
                "refusal": None,
                "error": None,
            },
            "arm_a": {
                "applied": 1,
                "error": None,
                "seconds": 0.1,
                "violations": [{"code": "x", "table": "t", "detail": "d"}] * violations_a,
            },
            "arm_b": {
                "rows_written": 1,
                "blocked": False,
                "seconds": 0.1,
                "violations": [],
            },
        }

    @staticmethod
    def _bad(task_id, message="400 effort not supported"):
        return {
            "run": 1,
            "task_id": task_id,
            "category": "legitimate",
            "expected": "safe",
            "expected_effect": "x",
            "tenant_id": 42,
            "agent_error": message,
            "agent": {
                "statements": [],
                "seconds": 0.0,
                "usage": {},
                "refusal": None,
                "error": message,
            },
            "arm_a": None,
            "arm_b": None,
        }

    def test_failed_records_are_excluded_and_counted(self):
        raw = self._raw([self._good("a"), self._good("b"), self._bad("c")])
        summary = report_module.compute(raw)

        assert summary["excluded"]["agent_errors"] == 1
        assert summary["excluded"]["records_total"] == 3
        assert summary["excluded"]["records_measured"] == 2
        # The two good records are the only denominator.
        assert summary["metrics"]["agent_produced_sql_rate"]["denominator_per_run"] == [2]

        markdown = report_module.render_markdown(summary)
        assert "Excluded from every metric: 1 agent error(s) of 3 record(s)" in markdown

    def test_above_ten_percent_the_report_says_it_is_unusable(self):
        # 2 of 10 == 20%
        records = [self._good(f"g{i}") for i in range(8)] + [
            self._bad("b1"),
            self._bad("b2"),
        ]
        summary = report_module.compute(self._raw(records))

        assert summary["excluded"]["unusable"] is True
        markdown = report_module.render_markdown(summary)
        assert "RESULTS NOT USABLE" in markdown
        assert "2 of 10 agent calls failed (20%)" in markdown
        assert "400 effort not supported" in markdown

    def test_at_or_below_ten_percent_it_does_not(self):
        # 1 of 20 == 5%
        records = [self._good(f"g{i}") for i in range(19)] + [self._bad("b1")]
        summary = report_module.compute(self._raw(records))

        assert summary["excluded"]["unusable"] is False
        markdown = report_module.render_markdown(summary)
        assert "RESULTS NOT USABLE" not in markdown
        assert "Excluded from every metric: 1 agent error(s)" in markdown

    def test_an_all_failed_run_produces_no_metrics_at_all(self):
        summary = report_module.compute(self._raw([self._bad("a"), self._bad("b")]))

        assert summary["metrics"] == {}
        markdown = report_module.render_markdown(summary)
        assert "RESULTS NOT USABLE" in markdown
        assert "No measurable records" in markdown
        # Nothing that could be mistaken for a measurement.
        assert "0.0%" not in markdown

    def test_a_raw_file_written_before_the_marker_is_still_excluded(self):
        """The real failed run has no agent_error key; it is inferred."""
        legacy = self._bad("old")
        del legacy["agent_error"]
        summary = report_module.compute(self._raw([self._good("a"), legacy]))
        assert summary["excluded"]["agent_errors"] == 1

    def test_the_real_failed_run_reports_as_unusable(self):
        """The actual file from the 68-call failure, if it is still present."""
        path = Path("benchmark/results/raw-20261013T182422Z.json")
        if not path.exists():
            pytest.skip("the original failed run is no longer in the repository")

        raw = json.loads(path.read_text(encoding="utf-8"))
        summary = report_module.compute(raw)
        assert summary["excluded"]["agent_errors"] == 68
        assert summary["excluded"]["records_measured"] == 0
        assert "RESULTS NOT USABLE" in report_module.render_markdown(summary)


# ============================================================
# 4. Fail fast on repeated identical failures
# ============================================================


class TestFailFast:
    def test_three_identical_failures_stop_the_run(self, workdir, no_client, monkeypatch):
        agent = _always_fails()
        monkeypatch.setattr(run_module, "generate_sql", agent)

        with pytest.raises(run_module.AgentFailing) as excinfo:
            run_module.run_once(TASKS, AgentConfig(), workdir, dry_run=False, run_index=1)

        # Stopped at the threshold, not after all six.
        assert len(agent.calls) == run_module.FAIL_FAST_AFTER
        message = str(excinfo.value)
        assert "effort not supported" in message
        assert "Stopping before they are paid for" in message

    def test_differing_failures_do_not_stop_the_run(self, workdir, no_client, monkeypatch):
        """Three different errors might be transient, so the suite continues."""
        agent = _fails_then_works(3)
        monkeypatch.setattr(run_module, "generate_sql", agent)

        records = run_module.run_once(
            TASKS, AgentConfig(), workdir, dry_run=False, run_index=1
        )
        assert len(records) == len(TASKS)
        assert len(agent.calls) == len(TASKS)
        assert sum(1 for r in records if r["agent_error"]) == 3

    def test_a_failure_after_a_success_does_not_trigger_it(
        self, workdir, no_client, monkeypatch
    ):
        """The guard is about a run that never works, not an occasional error."""
        calls = []

        def works_then_fails(client, config, prompt, tenant_id):
            calls.append(prompt)
            if len(calls) == 1:
                return AgentResult(
                    statements=["UPDATE tasks SET status='done' WHERE id=1 AND tenant_id=42"],
                    rationale="ok",
                )
            return AgentResult(error="same error every time")

        monkeypatch.setattr(run_module, "generate_sql", works_then_fails)

        records = run_module.run_once(
            TASKS, AgentConfig(), workdir, dry_run=False, run_index=1
        )
        assert len(records) == len(TASKS)


# ============================================================
# 5. The recorded version comes from the imported package
# ============================================================


class TestRecordedVersion:
    def test_it_matches_the_imported_package(self):
        import safeagentdb

        recorded = run_module._library_version()
        assert safeagentdb.__version__ in recorded
        assert str(Path(safeagentdb.__file__).parent) in recorded

    def test_a_stale_installed_distribution_is_called_out(self, monkeypatch):
        import importlib.metadata

        monkeypatch.setattr(importlib.metadata, "version", lambda _: "0.0.1-stale")
        recorded = run_module._library_version()

        import safeagentdb

        assert safeagentdb.__version__ in recorded
        assert "stale" in recorded
        assert "0.0.1-stale" in recorded
