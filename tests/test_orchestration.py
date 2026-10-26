from __future__ import annotations

import pytest

from app.models import Limits
from app.providers.demo import DemoProvider
from app.store import RunStore
from tests.conftest import trace_kinds

GOAL = "Implement tag normalization so the failing tests in the sample project pass"


async def test_demo_run_completes_with_full_evidence(build_run):
    orchestrator, runtime, record = build_run(
        DemoProvider(), goal=GOAL, use_sample=True
    )
    result = await orchestrator.run(record)
    run = result.record

    assert run.status == "completed"
    assert run.provider == "demo"
    assert run.plan is not None
    assert [step.id for step in run.plan.steps] == [
        "inspect_tests",
        "draft_implementation",
        "document_change",
    ]
    assert run.review is not None and run.review.verdict == "approve"
    assert run.verification is not None
    assert run.verification.passed is True
    assert run.verification.tests_passed == 3
    assert run.verification.tests_failed == 0
    assert run.verification.exit_code == 0
    assert set(run.changed_files) == {"target.py", "docs/change-notes.md"}
    assert "def normalize_tags" in run.artifacts["target.py"]
    assert "NotImplementedError" not in run.artifacts["target.py"]

    kinds = trace_kinds(result)
    for expected in [
        "run_started",
        "routing_decision",
        "agent_invocation",
        "verification",
        "run_finished",
    ]:
        assert expected in kinds

    invocations = [event for event in result.events if event.kind == "agent_invocation"]
    assert len(invocations) == 5  # planner + 3 coders + reviewer
    assert {event.payload["invocation"]["role"] for event in invocations} == {
        "planner",
        "coder",
        "reviewer",
    }
    assert runtime.ledger.calls == 5
    assert runtime.ledger.denied == 0


async def test_demo_plan_waves_are_explicit(build_run):
    orchestrator, _, record = build_run(DemoProvider(), goal=GOAL, use_sample=True)
    result = await orchestrator.run(record)

    waves = [[step.id for step in wave] for wave in result.record.plan.waves()]
    assert waves == [["inspect_tests", "draft_implementation"], ["document_change"]]


async def test_run_is_persisted_with_events(build_run, tmp_path):
    store = RunStore(tmp_path / "runs.db")
    try:
        orchestrator, _, record = build_run(
            DemoProvider(), goal=GOAL, use_sample=True, store=store
        )
        store.save_run(record)  # trace events have a foreign key to the run row
        result = await orchestrator.run(record)
        store.save_run(result.record)

        stored = store.get_run(result.record.run_id)
        assert stored is not None
        assert stored.status == "completed"
        assert stored.plan is not None
        assert stored.verification is not None and stored.verification.passed

        events = store.get_events(result.record.run_id)
        assert len(events) == len(result.events)
        assert [event.seq for event in events] == sorted(event.seq for event in events)

        listed = store.list_runs()
        assert [item.run_id for item in listed] == [result.record.run_id]
    finally:
        store.close()


async def test_review_rejection_triggers_one_revision(build_run):
    import json

    from tests.conftest import (
        PLAN_THREE_STEPS,
        SEED_OK,
        ScriptedProvider,
        coder_response,
        plan_response,
        review_response,
    )

    review_calls = {"count": 0}

    def handler(request, index):
        if request.role == "planner":
            return plan_response(PLAN_THREE_STEPS)
        if request.role == "coder":
            step_id = json.loads(request.user)["step"]["id"]
            return coder_response(step_id)
        review_calls["count"] += 1
        if review_calls["count"] == 1:
            return review_response(
                "request_changes",
                [
                    {
                        "severity": "major",
                        "step_id": "alpha",
                        "file": "out/alpha.txt",
                        "message": "alpha needs to be clearer",
                    }
                ],
            )
        return review_response("approve")

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(
        provider,
        files=SEED_OK,
        limits=Limits(max_revisions=1, max_escalations=0),
    )
    result = await orchestrator.run(record)

    assert result.record.status == "completed"
    assert result.record.revisions == 1
    assert any(event.kind == "revision" for event in result.events)
    coder_calls = [call for call in provider.calls if call.role == "coder"]
    assert len(coder_calls) == 4  # alpha, beta, gamma, then the alpha revision


@pytest.mark.parametrize("max_revisions", [0, 1])
async def test_rejections_stop_at_the_revision_limit(build_run, max_revisions):
    import json

    from tests.conftest import (
        PLAN_THREE_STEPS,
        SEED_OK,
        ScriptedProvider,
        coder_response,
        plan_response,
        review_response,
    )

    def handler(request, index):
        if request.role == "planner":
            return plan_response(PLAN_THREE_STEPS)
        if request.role == "coder":
            step_id = json.loads(request.user)["step"]["id"]
            return coder_response(step_id)
        return review_response(
            "request_changes",
            [{"severity": "blocker", "step_id": "alpha", "message": "still wrong"}],
        )

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(
        provider,
        files=SEED_OK,
        limits=Limits(max_revisions=max_revisions, max_escalations=0),
    )
    result = await orchestrator.run(record)

    assert result.record.status == "completed_with_findings"
    assert result.record.revisions == max_revisions + 1
    reviews = [call for call in provider.calls if call.role == "reviewer"]
    assert len(reviews) == max_revisions + 1