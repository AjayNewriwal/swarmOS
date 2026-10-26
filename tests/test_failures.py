from __future__ import annotations

import json

from app.models import Limits
from tests.conftest import (
    TARGET_IMPLEMENTATION,
    ScriptedProvider,
    coder_response,
    plan_response,
    review_response,
)

SINGLE_STEP_PLAN = {
    "steps": [
        {
            "id": "solo",
            "title": "Fix the target",
            "detail": "implement normalize_tags",
            "acceptance_criteria": ["tests pass"],
            "depends_on": [],
            "target_files": ["target.py"],
        }
    ]
}

COUNT_PLAN = {
    "steps": [
        {
            "id": "solo",
            "title": "Do the only step",
            "detail": "write a file",
            "acceptance_criteria": ["it exists"],
            "depends_on": [],
            "target_files": ["out/solo.txt"],
        }
    ]
}

BROKEN = {
    "step_id": "solo",
    "summary": "attempted the fix",
    "rationale": "",
    "changes": [
        {
            "path": "target.py",
            "content": "def normalize_tags(tags):\n    raise NotImplementedError\n",
            "action": "update",
        }
    ],
}


async def test_verification_failure_triggers_a_bounded_repair_round(build_run):
    coder_calls = {"count": 0}

    def handler(request, index):
        if request.role == "planner":
            return plan_response(SINGLE_STEP_PLAN)
        if request.role == "coder":
            coder_calls["count"] += 1
            payload = json.loads(request.user)
            if payload.get("verification_stdout"):
                return json.dumps(
                    {
                        "step_id": "solo",
                        "summary": "fixed after a failing verification run",
                        "rationale": "addresses the failing assertion",
                        "changes": [
                            {
                                "path": "target.py",
                                "content": TARGET_IMPLEMENTATION,
                                "action": "update",
                            }
                        ],
                    }
                )
            return json.dumps(BROKEN)
        return review_response("approve")

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(
        provider,
        use_sample=True,
        limits=Limits(max_retries=2, max_revisions=1, max_escalations=0),
    )
    result = await orchestrator.run(record)

    assert coder_calls["count"] == 2
    assert result.state["verification_attempts"] == 2
    assert result.record.status == "completed"
    assert result.record.verification is not None
    assert result.record.verification.passed is True
    assert result.record.verification.tests_passed == 3
    failures = [event for event in result.events if event.kind == "failure"]
    assert any("Verification reported failing tests" in event.message for event in failures)


async def test_repair_rounds_are_capped(build_run):
    def handler(request, index):
        if request.role == "planner":
            return plan_response(SINGLE_STEP_PLAN)
        if request.role == "coder":
            return json.dumps(BROKEN)
        return review_response("approve")

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(
        provider,
        use_sample=True,
        limits=Limits(max_retries=1, max_revisions=1, max_escalations=0),
    )
    result = await orchestrator.run(record)

    # initial attempt + one repair
    assert result.state["verification_attempts"] == 2
    assert result.record.status == "failed"
    assert "verification failed" in (result.record.error or "").lower()


async def test_unparseable_model_output_fails_cleanly(build_run):
    def handler(request, index):
        if request.role == "planner":
            return plan_response(COUNT_PLAN)
        if request.role == "coder":
            return "I refuse to answer in JSON."
        return review_response()

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(
        provider, limits=Limits(max_retries=0, max_escalations=0)
    )
    result = await orchestrator.run(record)

    assert result.record.status == "failed"
    assert result.record.error
    messages = " ".join(event.message for event in result.events)
    assert "unparseable" in messages.lower() or "json" in messages.lower()


async def test_coder_answering_for_the_wrong_step_is_rejected(build_run):
    def handler(request, index):
        if request.role == "planner":
            return plan_response(COUNT_PLAN)
        if request.role == "coder":
            return coder_response("some_other_step")
        return review_response()

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(
        provider, limits=Limits(max_retries=0, max_escalations=0)
    )
    result = await orchestrator.run(record)

    assert result.record.status == "failed"
    assert "some_other_step" in (result.record.error or "") or any(
        "some_other_step" in json.dumps(event.payload) for event in result.events
    )


async def test_planner_failure_ends_the_run_without_coding(build_run):
    def handler(request, index):
        return "not a plan"

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(
        provider, limits=Limits(max_retries=0, max_escalations=0)
    )
    result = await orchestrator.run(record)

    assert result.record.status == "failed"
    assert "Planner failed" in (result.record.error or "")
    assert not any(call.role == "coder" for call in provider.calls)