from __future__ import annotations

import json

from app.models import Limits, Plan
from app.providers.base import ProviderError
from tests.conftest import (
    PLAN_THREE_STEPS,
    ScriptedProvider,
    coder_response,
    plan_response,
    review_response,
)

SINGLE_STEP_PLAN = {
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

CONFLICT_PLAN = {
    "steps": [
        {
            "id": "left",
            "title": "Left writer",
            "detail": "write clash.txt",
            "acceptance_criteria": ["clash exists"],
            "depends_on": [],
            "target_files": ["clash.txt"],
        },
        {
            "id": "right",
            "title": "Right writer",
            "detail": "also write clash.txt",
            "acceptance_criteria": ["clash exists"],
            "depends_on": [],
            "target_files": ["clash.txt"],
        },
    ]
}


def step_id_of(request) -> str:
    return json.loads(request.user)["step"]["id"]


async def test_ready_steps_excludes_incomplete_dependencies(build_run):
    orchestrator, _, _ = build_run(ScriptedProvider(lambda request, index: ""))
    plan = Plan.model_validate(PLAN_THREE_STEPS)

    ready = orchestrator._ready_steps({"plan": plan, "completed_steps": ["alpha"]})
    assert [step.id for step in ready] == ["beta"]

    ready = orchestrator._ready_steps(
        {"plan": plan, "completed_steps": ["alpha", "beta"]}
    )
    assert [step.id for step in ready] == ["gamma"]


async def test_independent_batch_picks_the_earliest_wave(build_run):
    orchestrator, _, _ = build_run(ScriptedProvider(lambda request, index: ""))
    plan = Plan.model_validate(PLAN_THREE_STEPS)

    batch = orchestrator._independent_batch(plan, ["gamma", "alpha"])
    assert [step.id for step in batch] == ["alpha"]


async def test_after_planner_fans_out_only_independent_steps(build_run):
    orchestrator, _, _ = build_run(ScriptedProvider(lambda request, index: ""))
    plan = Plan.model_validate(PLAN_THREE_STEPS)
    state = {
        "plan": plan,
        "workspace": {},
        "attempts": {},
        "goal": "g",
        "run_id": "run",
        "model_tier": "fast",
    }

    sends = orchestrator._after_planner(state)
    assert isinstance(sends, list)
    assert {send.arg["step"]["id"] for send in sends} == {"alpha", "beta"}
    assert {send.node for send in sends} == {"code_step"}


async def test_independent_steps_run_concurrently_and_dependent_steps_wait(build_run):
    started: list[str] = []

    def handler(request, index):
        if request.role == "planner":
            return plan_response(PLAN_THREE_STEPS)
        if request.role == "coder":
            step_id = step_id_of(request)
            started.append(step_id)
            return coder_response(step_id)
        return review_response()

    provider = ScriptedProvider(handler, delay=0.02)
    orchestrator, _, record = build_run(provider)
    result = await orchestrator.run(record)

    assert result.record.status == "completed"
    assert provider.max_in_flight >= 2, "independent steps should overlap"
    # Both parallel steps must start before the dependent step is dispatched.
    assert started.index("gamma") > started.index("alpha")
    assert started.index("gamma") > started.index("beta")


async def test_conflicting_parallel_writes_are_rejected(build_run):
    def handler(request, index):
        if request.role == "planner":
            return plan_response(CONFLICT_PLAN)
        if request.role == "coder":
            step_id = step_id_of(request)
            return json.dumps(
                {
                    "step_id": step_id,
                    "summary": "write",
                    "rationale": "",
                    "changes": [
                        {"path": "clash.txt", "content": step_id, "action": "create"}
                    ],
                }
            )
        return review_response()

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(
        provider, limits=Limits(max_retries=0, max_escalations=0)
    )
    result = await orchestrator.run(record)

    assert result.record.status == "failed"
    assert "conflict" in (result.record.error or "").lower()
    messages = " ".join(event.message for event in result.events)
    assert "conflicting parallel writes" in messages.lower()


async def test_retries_are_bounded_then_escalated_then_stopped(build_run):
    def handler(request, index):
        if request.role == "planner":
            return plan_response(SINGLE_STEP_PLAN)
        if request.role == "coder":
            raise ProviderError("model unavailable")
        return review_response()

    provider = ScriptedProvider(handler)
    orchestrator, runtime, record = build_run(
        provider,
        limits=Limits(max_retries=1, max_escalations=1),
        base_url="http://127.0.0.1:1/v1",
        fast_model="fast-model",
        strong_model="strong-model",
    )
    result = await orchestrator.run(record)

    coder_calls = [call for call in provider.calls if call.role == "coder"]
    # initial attempt + one retry + one escalated attempt
    assert len(coder_calls) == 3
    assert result.record.status == "failed"
    assert result.record.escalations == 1
    assert provider.calls[-1].model == runtime.settings.model_for("strong")
    assert any(event.kind == "escalation" for event in result.events)
    assert any(event.kind == "retry" for event in result.events)
    assert result.state["attempts"]["solo"] == 3


async def test_no_escalation_when_disabled(build_run):
    def handler(request, index):
        if request.role == "planner":
            return plan_response(SINGLE_STEP_PLAN)
        if request.role == "coder":
            raise ProviderError("nope")
        return review_response()

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(
        provider, limits=Limits(max_retries=1, max_escalations=0)
    )
    result = await orchestrator.run(record)

    coder_calls = [call for call in provider.calls if call.role == "coder"]
    assert len(coder_calls) == 2
    assert result.record.status == "failed"
    assert result.record.escalations == 0