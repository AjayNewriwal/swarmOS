"""Focused tests for the deterministic policy benchmark.

They pin the measured metrics, the unavailable-metric policy, the JSON/Markdown
outputs, and the guarantees the benchmark must not break: a rejection is never
approval, and revision and call limits still bind.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.benchmark import (
    POLICIES,
    BenchmarkResult,
    build_report,
    config_fingerprint,
    render_markdown,
    result_from_run,
    summarise,
    write_json_report,
    write_markdown,
)
from app.benchmark_scenarios import (
    SCENARIOS,
    benchmark_settings,
    run_scenario,
    scenario_report_config,
)
from app.models import Limits, RunRecord
from app.store import RunStore
from tests.conftest import (
    ScriptedProvider,
    coder_response,
    make_settings,
    plan_response,
    review_response,
)

SCENARIO_ORDER = [
    "first_try_success",
    "revision_required",
    "genuine_blocker",
    "insufficient_evidence",
    "malformed_review",
    "planner_malformed",
    "coder_malformed",
    "reviewer_malformed",
    "provider_timeout",
    "verification_failure",
    "verification_timeout",
    "execution_disabled",
    "escalation_recovery",
    "escalation_limit",
    "escalation_unavailable",
    "revision_limit_hit",
    "call_limit",
]


def _settings(tmp_path):
    return benchmark_settings(make_settings(tmp_path))


async def _run(name: str, policy: str, settings, store: RunStore) -> BenchmarkResult:
    return await run_scenario(
        SCENARIOS[name], POLICIES[policy], settings=settings, store=store
    )


def _stable(item: BenchmarkResult) -> tuple:
    return (
        item.status,
        item.review_verdict,
        item.verification_passed,
        item.agent_calls,
        item.planner_calls,
        item.coder_calls,
        item.reviewer_calls,
        item.revisions,
        item.reviewer_rejections,
        item.invalid_reviews,
        item.evidence_gap_events,
        item.escalations,
        tuple(item.escalation_triggers),
        tuple(sorted(item.models_used.items())),
    )


async def test_first_try_success_records_exact_metrics(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        item = await _run("first_try_success", "fixed-fast", settings, store)
    finally:
        store.close()

    assert item.task_id == "first_try_success"
    assert item.policy == "fixed-fast"
    assert item.run_id
    assert item.provider == "scripted"
    assert item.simulated is True
    assert item.status == "completed"
    assert item.review_verdict == "approve"
    assert item.planner_calls == 1
    assert item.coder_calls == 1
    assert item.reviewer_calls == 1
    assert item.agent_calls == 3
    assert item.revisions == 0
    assert item.reviewer_rejections == 0
    assert item.verification_passed is True
    assert item.verification_attempts == 1
    assert item.verification_gating is True
    assert item.verification_exit_code == 0
    # The scripted provider reports no usage, so tokens and cost are unknown.
    assert item.usage_available is False
    assert item.input_tokens is None
    assert item.output_tokens is None
    assert item.cost_usd is None


async def test_scripted_runs_are_repeatable(tmp_path):
    settings = _settings(tmp_path)
    first_store = RunStore(":memory:")
    second_store = RunStore(":memory:")
    try:
        first = await _run("revision_required", "adaptive", settings, first_store)
        second = await _run("revision_required", "adaptive", settings, second_store)
    finally:
        first_store.close()
        second_store.close()

    assert _stable(first) == _stable(second)
    assert first.status == "completed"
    assert first.coder_calls == 2
    assert first.reviewer_calls == 2
    assert first.revisions == 1
    assert first.reviewer_rejections == 1
    assert first.evidence_gap_events == 0


async def test_genuine_blocker_is_recorded_not_approved(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        item = await _run("genuine_blocker", "fixed-fast", settings, store)
    finally:
        store.close()

    assert item.status == "completed_with_findings"
    assert item.review_verdict == "request_changes"
    assert item.revisions == 1
    assert item.reviewer_rejections == 2
    assert item.evidence_gap_events == 0
    assert item.verification_diagnostic is True
    assert item.verification_passed is False


async def test_insufficient_evidence_is_a_gap_not_an_approval(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        item = await _run("insufficient_evidence", "fixed-fast", settings, store)
    finally:
        store.close()

    # Still a rejection: the evidence check must not become automatic approval.
    assert item.status == "completed_with_findings"
    assert item.review_verdict == "request_changes"
    assert item.evidence_gap_events == 2  # one per review round
    assert item.invalid_reviews == 0
    assert item.verification_passed is False


async def test_malformed_review_is_invalid_and_dispatches_nothing(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        item = await _run("malformed_review", "fixed-fast", settings, store)
    finally:
        store.close()

    assert item.status == "review_invalid"
    assert item.review_verdict == "request_changes"
    assert item.invalid_reviews == 1
    assert item.revisions == 0
    assert item.coder_calls == 1
    assert item.reviewer_calls == 1


async def test_verification_failure_records_every_attempt(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        item = await _run("verification_failure", "fixed-fast", settings, store)
    finally:
        store.close()

    assert item.status == "failed"
    assert item.verification_passed is False
    assert item.verification_attempts == 2  # initial verification + one repair
    assert item.revisions == 0


async def test_revision_and_call_limits_bind(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        limited = await _run("revision_limit_hit", "adaptive", settings, store)
        capped = await _run("call_limit", "adaptive", settings, store)
    finally:
        store.close()

    assert SCENARIOS["revision_limit_hit"].limits.max_revisions == 0
    assert limited.status == "completed_with_findings"
    assert limited.revisions == 0
    assert limited.coder_calls == 1
    assert limited.reviewer_calls == 1

    assert capped.status == "limit_exceeded"
    assert capped.limit_exhausted is True
    assert capped.denied_calls >= 1
    assert capped.verification_attempts == 0


async def test_escalation_recovery_is_measured_per_policy(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        fixed = await _run("escalation_recovery", "fixed-fast", settings, store)
        adaptive = await _run("escalation_recovery", "adaptive", settings, store)
    finally:
        store.close()

    # The fast profile cannot fix the code; the fixed policy may not escalate.
    assert fixed.status == "completed_with_findings"
    assert fixed.escalations == 0
    assert fixed.models_used["coder"] == ["bench-weak"]
    assert fixed.escalation_triggers == []
    # The adaptive policy spends one escalation and recovers.
    assert adaptive.status == "completed"
    assert adaptive.escalations == 1
    assert adaptive.agent_calls > fixed.agent_calls
    # The report shows both models the coder actually used.
    assert adaptive.models_used["coder"] == ["bench-strong", "bench-weak"]
    assert adaptive.escalation_triggers == ["revision_budget_exhausted"]
    assert adaptive.models_by_role["coder"] == "bench-weak"  # starting selection


async def test_escalation_triggers_distinguish_failure_reasons(tmp_path):
    settings = _settings(tmp_path)
    limits = Limits(
        max_agent_calls=12,
        max_budget_usd=0.25,
        max_wall_seconds=120,
        max_retries=0,
        max_revisions=0,
        max_escalations=1,
    )
    store = RunStore(":memory:")
    try:
        verify_case = replace(SCENARIOS["verification_failure"], limits=limits)
        coding_case = replace(SCENARIOS["coder_malformed"], limits=limits)
        verify_item = await run_scenario(
            verify_case, POLICIES["adaptive"], settings=settings, store=store
        )
        coding_item = await run_scenario(
            coding_case, POLICIES["adaptive"], settings=settings, store=store
        )
    finally:
        store.close()

    assert verify_item.escalation_triggers == ["verification_failed"]
    assert coding_item.escalation_triggers == ["coding_failures_exhausted_retries"]


async def test_measured_usage_and_cost_are_reported(build_run):
    """When a provider reports usage and pricing is configured, cost is measured."""
    plan = {
        "steps": [
            {
                "id": "alpha",
                "title": "Create alpha",
                "detail": "Write out/alpha.txt",
                "acceptance_criteria": ["out/alpha.txt exists and is readable"],
                "depends_on": [],
                "target_files": ["out/alpha.txt"],
            }
        ]
    }

    def handler(request, index):
        if request.role == "planner":
            return plan_response(plan)
        if request.role == "coder":
            return coder_response("alpha")
        return review_response("approve")

    provider = ScriptedProvider(
        handler, input_tokens=1000, output_tokens=500, usage_available=True
    )
    orchestrator, _, record = build_run(
        provider,
        limits=Limits(max_revisions=0, max_retries=0, max_escalations=0),
        base_url="http://127.0.0.1:11434/v1",
        fast_model="priced-fast",
        fast_input_rate=1.0,
        fast_output_rate=2.0,
    )
    result = await orchestrator.run(record)
    item = result_from_run(
        task_id="priced",
        policy="fixed-fast",
        record=result.record,
        events=result.events,
        escalation_available=False,
    )

    # Three calls (planner, coder, reviewer), each reporting 1000/500 tokens.
    expected_cost = 3 * ((1000 * 1.0) + (500 * 2.0)) / 1_000_000
    assert result.record.usage_available is True
    assert result.record.monetary_enforced is True
    assert result.record.cost_usd == pytest.approx(expected_cost)
    assert item.usage_available is True
    assert item.input_tokens == 3000
    assert item.output_tokens == 1500
    assert item.cost_usd == pytest.approx(expected_cost)
    assert item.monetary_enforced is True


async def test_persisted_benchmark_runs_do_not_disturb_existing_records(tmp_path):
    store = RunStore(tmp_path / "bench.db")
    existing = RunRecord(goal="unrelated production run", status="completed")
    try:
        store.save_run(existing)
        item = await _run("first_try_success", "fixed-fast", _settings(tmp_path), store)
        stored = store.get_run(existing.run_id)
        assert stored is not None
        assert stored.goal == "unrelated production run"
        assert stored.status == "completed"
        assert store.get_run(item.run_id) is not None
    finally:
        store.close()


async def test_outcome_invariants_hold_across_outcomes(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        results = [
            await _run(name, "adaptive", settings, store)
            for name in ("first_try_success", "genuine_blocker", "malformed_review")
        ]
    finally:
        store.close()

    by_name = {item.task_id: item for item in results}
    approved = by_name["first_try_success"]
    assert approved.status == "completed"
    assert approved.review_verdict == "approve"
    assert approved.verification_passed is True
    assert approved.verification_gating is True

    rejected = by_name["genuine_blocker"]
    assert rejected.status == "completed_with_findings"
    assert rejected.review_verdict == "request_changes"
    assert rejected.status != "completed"

    invalid = by_name["malformed_review"]
    assert invalid.status == "review_invalid"
    assert invalid.review_verdict == "request_changes"
    assert invalid.invalid_reviews >= 1
    assert invalid.status != "completed"


async def test_planner_malformed_output_fails_without_coding(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        item = await _run("planner_malformed", "fixed-fast", settings, store)
    finally:
        store.close()

    assert item.status == "failed"
    assert item.planner_calls == 2  # initial attempt + one bounded repair
    assert item.coder_calls == 0
    assert item.reviewer_calls == 0
    assert item.verification_attempts == 0


async def test_coder_malformed_output_is_bounded(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        item = await _run("coder_malformed", "adaptive", settings, store)
    finally:
        store.close()

    assert item.status == "failed"
    assert item.coder_calls == 2  # one retry, then the run stops
    assert item.reviewer_calls == 0
    assert item.verification_attempts == 0


async def test_reviewer_malformed_output_is_not_approval(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        item = await _run("reviewer_malformed", "fixed-fast", settings, store)
    finally:
        store.close()

    assert item.status == "failed"
    assert item.review_verdict is None
    assert item.reviewer_calls == 2  # initial attempt + one bounded repair
    assert item.verification_attempts == 0  # never treated as an approval


async def test_transient_provider_timeout_recovers_within_the_limit(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        item = await _run("provider_timeout", "fixed-fast", settings, store)
    finally:
        store.close()

    assert item.status == "completed"
    assert item.coder_calls == 2
    assert item.retries == 1
    assert item.verification_passed is True


async def test_escalation_limit_stops_after_one_round(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        item = await _run("escalation_limit", "adaptive", settings, store)
    finally:
        store.close()

    # The strong model is wrong too: one escalation, no second attempt.
    assert item.status == "completed_with_findings"
    assert item.escalations == 1
    assert item.revisions == 1
    assert item.reviewer_calls == 2
    assert item.review_verdict == "request_changes"


async def test_escalation_unavailable_is_recorded_not_faked(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        item = await _run("escalation_unavailable", "adaptive", settings, store)
        summary = summarise([item])
    finally:
        store.close()

    assert item.escalation_available is False
    assert item.escalations == 0
    assert item.status == "completed_with_findings"
    assert any("Escalation is inert" in warning for warning in summary["warnings"])


async def test_verification_timeout_and_disabled_execution_are_recorded(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        timed_out = await _run("verification_timeout", "fixed-fast", settings, store)
        disabled = await _run("execution_disabled", "fixed-fast", settings, store)
    finally:
        store.close()

    assert timed_out.status == "failed"
    assert timed_out.verification_passed is False
    assert timed_out.verification_gating is True
    assert timed_out.verification_exit_code == 124

    assert disabled.status == "failed"
    assert disabled.verification_passed is False
    assert disabled.verification_exit_code == 126


async def test_run_ids_are_unique_per_run(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        first = await _run("first_try_success", "fixed-fast", settings, store)
        second = await _run("first_try_success", "fixed-fast", settings, store)
    finally:
        store.close()

    assert first.run_id != second.run_id
    assert _stable(first) == _stable(second)


async def test_runs_do_not_touch_repository_or_scenario_files(tmp_path):
    settings = _settings(tmp_path)
    project = settings.sample_project
    before = {path: path.read_text(encoding="utf-8") for path in project.rglob("*.py")}
    scenarios_before = {
        name: dict(scenario.files) for name, scenario in SCENARIOS.items() if scenario.files
    }

    store = RunStore(":memory:")
    try:
        await _run("first_try_success", "fixed-fast", settings, store)
    finally:
        store.close()

    after = {path: path.read_text(encoding="utf-8") for path in project.rglob("*.py")}
    assert after == before
    assert (
        {name: dict(scenario.files) for name, scenario in SCENARIOS.items() if scenario.files}
        == scenarios_before
    )


def _result(**overrides) -> BenchmarkResult:
    base = {
        "run_id": "run_fixture",
        "task_id": "task_a",
        "policy": "fixed-fast",
        "provider": "scripted",
        "provider_kind": "unknown",
        "model_policy": "fast_only",
        "status": "completed",
        "verification_passed": True,
        "verification_attempts": 1,
        "agent_calls": 3,
        "duration_ms": 1000,
    }
    base.update(overrides)
    return BenchmarkResult(**base)


def test_aggregation_counts_outcomes_and_withholds_unmeasured_cost():
    results = [
        _result(),
        _result(
            run_id="run_b",
            policy="adaptive",
            status="failed",
            verification_passed=False,
            verification_attempts=2,
            agent_calls=5,
            revisions=1,
            reviewer_rejections=1,
            evidence_gap_events=2,
        ),
    ]
    summary = summarise(results)
    fixed = summary["policies"]["fixed-fast"]
    adaptive = summary["policies"]["adaptive"]

    assert fixed["runs"] == 1
    assert fixed["completed"] == 1
    assert fixed["success_rate"] == 1.0
    assert adaptive["completed"] == 0
    assert adaptive["failed"] == 1
    assert adaptive["success_rate"] == 0.0
    assert adaptive["verification_pass_rate"] == 0.0
    assert adaptive["total_rejections"] == 1
    assert adaptive["total_evidence_gaps"] == 2
    assert adaptive["mean_agent_calls"] == 5.0
    assert adaptive["mean_revisions"] == 1.0

    # No usage metadata: tokens and cost stay unavailable, never zero.
    assert summary["cost_comparable"] is False
    assert summary["policies"]["fixed-fast"]["input_tokens"] is None
    assert summary["policies"]["fixed-fast"]["cost_usd"] is None
    assert "withheld" in summary["cost_note"]


def test_aggregation_reports_measured_cost_when_usage_exists():
    measured = [
        _result(
            run_id=f"run_{policy}",
            policy=policy,
            usage_available=True,
            monetary_enforced=True,
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.0001,
            simulated=False,
            escalation_available=True,
        )
        for policy in ("fixed-fast", "adaptive", "multi-agent")
    ]
    summary = summarise(measured)

    assert summary["cost_comparable"] is True
    assert summary["cost_note"] == "cost comparison available"
    assert summary["policies"]["adaptive"]["input_tokens"] == 100
    assert summary["policies"]["adaptive"]["output_tokens"] == 50
    assert summary["policies"]["adaptive"]["cost_usd"] == 0.0001


def test_failed_run_without_events_reports_unknown_metrics():
    record = RunRecord(goal="Benchmark a failing run", status="failed", limits=Limits())
    item = result_from_run(
        task_id="task_a", policy="fixed-fast", record=record, events=[]
    )

    assert item.status == "failed"
    assert item.agent_calls == 0
    assert item.planner_calls == 0
    assert item.coder_calls == 0
    assert item.reviewer_calls == 0
    assert item.verification_attempts == 0
    assert item.input_tokens is None
    assert item.cost_usd is None
    assert item.error is None


async def test_report_outputs_are_valid_json_and_markdown(tmp_path):
    settings = _settings(tmp_path)
    store = RunStore(":memory:")
    try:
        results = [
            await _run("first_try_success", "fixed-fast", settings, store),
            await _run("first_try_success", "adaptive", settings, store),
        ]
    finally:
        store.close()

    config = scenario_report_config(settings, policies=["fixed-fast", "adaptive"])
    summary = summarise(results)
    report = build_report(results, suite="scripted-scenarios", config=config)

    json_path = tmp_path / "out" / "report.json"
    write_json_report(json_path, report)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["suite"] == "scripted-scenarios"
    assert payload["config"]["simulated"] is True
    assert len(payload["results"]) == 2
    assert {row["policy"] for row in payload["results"]} == {
        "fixed-fast",
        "adaptive",
    }
    assert payload["results"][0]["task_id"] == "first_try_success"
    assert payload["config_fingerprint"] == json.dumps(
        config, sort_keys=True, separators=(",", ":")
    )
    assert payload["summary"]["tradeoffs"]

    text = render_markdown(results, summary, config=config)
    markdown_path = tmp_path / "out" / "report.md"
    write_markdown(markdown_path, text)
    rendered = markdown_path.read_text(encoding="utf-8")
    assert "# SwarmOS policy benchmark" in rendered
    assert "## Results by task and policy" in rendered
    assert "## Aggregates by policy" in rendered
    assert "## Run identifiers" in rendered
    assert "## Metric definitions" in rendered
    assert "## Escalations" in rendered
    assert "## Trade-offs" in rendered
    assert "Scripted model responses are present" in rendered
    assert "Denominators:" in rendered
    assert "No policy is declared best" in rendered
    assert payload["results"][0]["run_id"] in rendered
    # The unmeasured cost is reported as unavailable, not as $0.
    assert "Unavailable." in rendered


def test_config_fingerprint_is_stable_and_complete(tmp_path):
    settings = _settings(tmp_path)
    fingerprint = config_fingerprint(settings)

    assert fingerprint["fast_model"] == settings.fast_model
    assert fingerprint["strong_model"] == settings.strong_model
    assert fingerprint["escalation_available"] is True
    assert fingerprint["monetary_enforced"] is False
    assert fingerprint["verification_timeout_seconds"] == (
        settings.verification_timeout_seconds
    )
    assert fingerprint["max_revisions"] == settings.max_revisions
    assert config_fingerprint(settings) == fingerprint


def test_scenario_config_records_limits_faults_and_settings(tmp_path):
    settings = _settings(tmp_path)
    config = scenario_report_config(
        settings,
        policies=["fixed-fast"],
        scenario_ids=["verification_timeout"],
        store="memory",
    )

    scenario = config["scenarios"]["verification_timeout"]
    assert scenario["limits"]["max_revisions"] == 0
    assert scenario["settings_overrides"] == {"verification_timeout_seconds": 1}
    assert config["settings"]["execution_enabled"] is True
    assert config["store"] == "memory"


def test_outcome_totals_account_for_every_run():
    results = [
        _result(run_id="run_1", status="completed"),
        _result(run_id="run_2", status="completed_with_findings"),
        _result(run_id="run_3", status="review_invalid"),
        _result(run_id="run_4", status="failed"),
        _result(run_id="run_5", status="limit_exceeded"),
        _result(run_id="run_6", status="running"),
    ]
    fixed = summarise(results)["policies"]["fixed-fast"]

    assert fixed["runs"] == 6
    assert sum(fixed["outcomes"].values()) == 6
    assert fixed["outcomes"]["other"] == 1
    assert fixed["completed"] == 1
    assert fixed["completed_with_findings"] == 1
    assert fixed["review_invalid"] == 1
    assert fixed["failed"] == 1
    assert fixed["limit_exceeded"] == 1
    assert fixed["success_rate"] == round(1 / 6, 3)


def test_diagnostic_verification_never_raises_the_pass_rate():
    results = [
        _result(
            run_id="run_rejected",
            status="completed_with_findings",
            verification_passed=True,
            verification_attempts=1,
            verification_diagnostic=True,
            verification_gating=False,
        )
    ]
    fixed = summarise(results)["policies"]["fixed-fast"]

    assert fixed["gating_verification_runs"] == 0
    assert fixed["verification_pass_rate"] is None
    assert fixed["diagnostic_verification_runs"] == 1
    assert fixed["verified"] == 1  # recorded, but not a gating pass


def test_by_task_keeps_policies_outside_the_known_order():
    results = [
        _result(run_id="run_known", policy="adaptive"),
        _result(run_id="run_custom", policy="custom-policy"),
    ]
    rows = summarise(results)["by_task"]

    assert {(row["task_id"], row["policy"]) for row in rows} == {
        ("task_a", "adaptive"),
        ("task_a", "custom-policy"),
    }
    assert {row["run_id"] for row in rows} == {"run_known", "run_custom"}


def test_json_and_markdown_agree_on_every_cell():
    results = [
        _result(run_id="run_a", policy="fixed-fast"),
        _result(
            run_id="run_b",
            policy="adaptive",
            status="completed_with_findings",
            verification_passed=False,
            verification_attempts=1,
            verification_gating=True,
            revisions=1,
            reviewer_rejections=1,
            evidence_gap_events=2,
        ),
    ]
    summary = summarise(results)
    rendered = render_markdown(
        results,
        summary,
        config={"suite": "fixture", "simulated": False},
    )

    for row in summary["by_task"]:
        expected = (
            f"| {row['task_id']} | {row['policy']} | {row['status']} "
            f"| {row['verification']} | {row['calls']} | {row['revisions']} "
            f"| {row['rejections']} | {row['invalid_reviews']} "
            f"| {row['evidence_gaps']} |"
        )
        assert expected in rendered


def test_metric_definitions_are_published_with_the_summary():
    definitions = summarise([_result()])["metric_definitions"]

    assert "success_rate" in definitions
    assert "gating verification" in definitions["verification_pass_rate"]
    assert "null" in definitions["cost_usd"]
