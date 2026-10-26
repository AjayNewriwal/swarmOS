from __future__ import annotations

import asyncio
import json
from pathlib import Path

from app.benchmark import (
    POLICIES,
    load_tasks,
    request_for,
    result_from_run,
    summarise,
    write_results,
)
from app.models import RunRequest
from app.service import RunService
from app.store import RunStore
from tests.conftest import make_settings

TASKS = Path(__file__).resolve().parent.parent / "benchmarks" / "tasks.jsonl"


def test_bundled_tasks_are_loadable_and_reproducible():
    tasks = load_tasks(TASKS)
    assert len(tasks) >= 3
    assert len({task.id for task in tasks}) == len(tasks)
    assert all(len(task.goal) >= 8 for task in tasks)


def test_policies_are_explicit_and_share_the_graph():
    assert set(POLICIES) == {"fixed-fast", "adaptive", "multi-agent"}
    assert POLICIES["fixed-fast"].max_escalations == 0
    assert POLICIES["fixed-fast"].model_policy == "fast_only"
    assert POLICIES["multi-agent"].model_policy == "role_routed"


def test_request_for_applies_policy_and_limits():
    task = load_tasks(TASKS)[0]
    request = request_for(task, POLICIES["fixed-fast"], "demo", 0.0)
    assert isinstance(request, RunRequest)
    assert request.model_policy == "fast_only"
    assert request.limits.max_escalations == 0
    assert request.metadata["benchmark_task"] == task.id


def test_summary_withholds_cost_when_usage_is_missing(tmp_path):
    store = RunStore(tmp_path / "bench.db")
    settings = make_settings(tmp_path, provider="demo")
    service = RunService(settings, store)
    try:
        task = load_tasks(TASKS)[0]
        results = []
        for name in ("fixed-fast", "adaptive"):
            request = request_for(task, POLICIES[name], "demo", 0.0)
            record = asyncio.run(service.run_request(request))
            results.append(
                result_from_run(
                    task_id=task.id,
                    policy=POLICIES[name].name,
                    record=record,
                    events=store.get_events(record.run_id),
                )
            )
        summary = summarise(results)

        assert all(item.status == "completed" for item in results)
        assert all(item.verification_passed for item in results)
        assert all(item.usage_available is False for item in results)
        assert all(item.cost_usd == 0 for item in results)
        assert summary["cost_comparable"] is False
        assert "withheld" in summary["cost_note"]
        assert "declared best" in summary["note"]
        assert set(summary["policies"]) == {"fixed-fast", "adaptive"}
    finally:
        store.close()


def test_results_are_written_as_jsonl(tmp_path):
    store = RunStore(tmp_path / "bench.db")
    settings = make_settings(tmp_path, provider="demo")
    service = RunService(settings, store)
    out = tmp_path / "results" / "run.jsonl"
    try:
        task = load_tasks(TASKS)[0]
        record = asyncio.run(
            service.run_request(request_for(task, POLICIES["fixed-fast"], "demo", 0.0))
        )
        item = result_from_run(
            task_id=task.id,
            policy="fixed-fast",
            record=record,
            events=store.get_events(record.run_id),
        )
        write_results(out, [item])

        lines = out.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["task_id"] == task.id
        assert payload["policy"] == "fixed-fast"
        assert payload["provider"] == "demo"
    finally:
        store.close()

def _result(policy: str, escalation_available: bool | None, **overrides):
    from app.benchmark import BenchmarkResult

    base = {
        "run_id": f"run_{policy}",
        "task_id": "t1",
        "policy": policy,
        "provider": "openai-compatible:local",
        "provider_kind": "local",
        "model_policy": "adaptive",
        "escalation_available": escalation_available,
        "status": "completed",
        "verification_passed": True,
        "agent_calls": 5,
        "duration_ms": 1000,
    }
    base.update(overrides)
    return BenchmarkResult(**base)


def test_summary_warns_when_escalation_cannot_select_a_stronger_model():
    results = [
        _result("fixed-fast", False),
        _result("adaptive", False),
        _result("multi-agent", False),
    ]
    summary = summarise(results)

    warnings = summary["warnings"]
    assert len(warnings) == 1
    text = warnings[0]
    assert "Escalation is inert" in text
    assert "adaptive" in text and "multi-agent" in text
    assert "not evidence that the policies perform equivalently" in text
    assert "OPENAI_COMPAT_MODEL_STRONG" in text
    # The non-escalating policy is not blamed for being unable to escalate.
    assert "fixed-fast" not in text


def test_summary_does_not_warn_when_a_stronger_model_is_available():
    results = [
        _result("fixed-fast", True),
        _result("adaptive", True),
        _result("multi-agent", True),
    ]
    summary = summarise(results)
    assert summary["warnings"] == []


def test_summary_reports_unknown_escalation_availability():
    results = [_result("adaptive", None)]
    summary = summarise(results)
    assert len(summary["warnings"]) == 1
    assert "was not recorded" in summary["warnings"][0]


def test_summary_warning_does_not_change_reported_metrics():
    inert = summarise([_result("adaptive", False)])
    available = summarise([_result("adaptive", True)])
    assert inert["policies"] == available["policies"]
    assert inert["cost_comparable"] == available["cost_comparable"]


def test_result_records_escalation_availability(tmp_path):
    store = RunStore(tmp_path / "bench.db")
    settings = make_settings(tmp_path, provider="demo")
    service = RunService(settings, store)
    try:
        task = load_tasks(TASKS)[0]
        record = asyncio.run(
            service.run_request(request_for(task, POLICIES["adaptive"], "demo", 0.0))
        )
        item = result_from_run(
            task_id=task.id,
            policy="adaptive",
            record=record,
            events=store.get_events(record.run_id),
            escalation_available=False,
        )
        assert item.escalation_available is False
    finally:
        store.close()
