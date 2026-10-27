"""Focused tests for the seeded sample_tag_normalization demo recipe."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from app.demo_tasks import (
    SAMPLE_ALLOWED_FILES,
    SAMPLE_READ_ONLY_FILES,
    SAMPLE_TASK_ID,
    DemoTaskError,
    resolve_demo_task,
)
from app.models import Limits, RunRequest
from app.service import RunService
from app.store import RunStore
from tests.conftest import (
    TARGET_IMPLEMENTATION,
    ScriptedProvider,
    make_settings,
)

BROKEN_TARGET = (
    "from collections.abc import Iterable\n\n\n"
    "def normalize_tags(tags: Iterable[str]) -> list[str]:\n"
    "    raise NotImplementedError\n"
)


def coder_output(step_id: str, changes: list[dict]) -> str:
    return json.dumps(
        {
            "step_id": step_id,
            "summary": "Implement normalize_tags",
            "rationale": "Matches the sample tests.",
            "changes": changes,
        }
    )


def approve() -> str:
    return json.dumps({"verdict": "approve", "rationale": "ok", "findings": []})


# ----------------------------------------------------------------------
# recipe shape
# ----------------------------------------------------------------------
def test_recipe_is_one_step_on_target_py_only():
    task = resolve_demo_task(SAMPLE_TASK_ID)

    assert task.id == SAMPLE_TASK_ID
    assert len(task.plan.steps) == 1
    step = task.plan.steps[0]
    assert step.target_files == ["target.py"]
    assert task.allowed_files == SAMPLE_ALLOWED_FILES == ("target.py",)
    assert task.read_only_files == SAMPLE_READ_ONLY_FILES
    assert "tests/test_target.py" in task.read_only_files

    assert task.limits.max_agent_calls == 6
    assert task.limits.max_retries == 0
    assert task.limits.max_revisions == 1
    assert task.limits.max_escalations == 0
    assert task.limits.max_wall_seconds == 120


def test_unknown_demo_task_is_rejected():
    with pytest.raises(DemoTaskError):
        resolve_demo_task("made_up_task")


def test_run_request_accepts_only_the_known_demo_task():
    request = RunRequest(goal="Implement the sample task", demo_task=SAMPLE_TASK_ID)
    assert request.demo_task == SAMPLE_TASK_ID

    with pytest.raises(ValidationError):
        RunRequest(goal="Implement the sample task", demo_task="not_a_task")


def test_demo_does_not_accept_custom_files(tmp_path):
    settings = make_settings(tmp_path, provider="demo")
    store = RunStore(":memory:")
    try:
        service = RunService(settings, store)
        with pytest.raises(DemoTaskError):
            service.prepare(
                RunRequest(
                    goal="Implement the sample task",
                    demo_task=SAMPLE_TASK_ID,
                    files={"target.py": "custom"},
                )
            )
    finally:
        store.close()


def test_demo_limits_cannot_be_raised(tmp_path):
    settings = make_settings(tmp_path, provider="demo")
    store = RunStore(":memory:")
    try:
        service = RunService(settings, store)
        record, _, _, demo = service.prepare(
            RunRequest(
                goal="ignored goal text",
                demo_task=SAMPLE_TASK_ID,
                provider="demo",
                limits=Limits(
                    max_agent_calls=12,
                    max_retries=3,
                    max_revisions=3,
                    max_escalations=1,
                    max_wall_seconds=300,
                ),
            )
        )
        assert demo is not None
        assert record.goal == demo.goal
        assert record.limits.max_agent_calls == 6
        assert record.limits.max_retries == 0
        assert record.limits.max_revisions == 1
        assert record.limits.max_escalations == 0
        assert record.limits.max_wall_seconds == 120
    finally:
        store.close()


# ----------------------------------------------------------------------
# seeded-plan routing
# ----------------------------------------------------------------------
def test_seeded_plan_skips_the_planner_model(build_run):
    def handler(request, index):
        assert request.role != "planner", "the planner must not be called"
        if request.role == "coder":
            return coder_output(
                "draft_implementation",
                [
                    {
                        "path": "target.py",
                        "content": TARGET_IMPLEMENTATION,
                        "action": "update",
                    }
                ],
            )
        return approve()

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(provider, demo_task=SAMPLE_TASK_ID)
    result = asyncio.run(orchestrator.run(record))

    assert result.record.status == "completed"
    assert result.record.plan is not None
    assert len(result.record.plan.steps) == 1
    assert result.record.plan.steps[0].target_files == ["target.py"]
    assert result.record.models_by_role["planner"] == "skipped (demo recipe)"
    assert all(call.role != "planner" for call in provider.calls)

    seeded = [
        event
        for event in result.events
        if event.kind == "routing_decision"
        and event.payload.get("decision") == "plan_seeded"
    ]
    assert len(seeded) == 1
    assert seeded[0].payload["planner_skipped"] is True
    assert "planner model skipped" in seeded[0].message
    assert seeded[0].payload["recipe"] == SAMPLE_TASK_ID

    roles = [
        event.payload["role"]
        for event in result.events
        if event.kind == "agent_invocation"
    ]
    assert "planner" not in roles
    assert roles.count("coder") == 1
    assert roles.count("reviewer") == 1


def test_seeded_demo_runs_real_verification_on_three_tests(tmp_path):
    store = RunStore(":memory:")
    settings = make_settings(tmp_path, provider="demo")
    service = RunService(settings, store)
    try:
        record = asyncio.run(
            service.run_request(
                RunRequest(
                    goal="ignored goal, the recipe supplies the real one",
                    provider="demo",
                    demo_task=SAMPLE_TASK_ID,
                )
            )
        )
        assert record.status == "completed"
        assert record.changed_files == ["target.py"]
        assert record.revisions == 0
        assert record.verification is not None
        assert record.verification.passed is True
        assert record.verification.tests_passed == 3
        assert record.verification.tests_failed == 0
        assert "pytest" in record.verification.command
    finally:
        store.close()


# ----------------------------------------------------------------------
# path enforcement
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    ["tests/test_target.py", "docs/notes.md", "other.py"],
)
def test_disallowed_writes_are_rejected_before_apply(build_run, path):
    def handler(request, index):
        assert request.role != "planner"
        if request.role == "coder":
            return coder_output(
                "draft_implementation",
                [
                    {
                        "path": "target.py",
                        "content": BROKEN_TARGET,
                        "action": "update",
                    },
                    {"path": path, "content": "tampered", "action": "update"},
                ],
            )
        return approve()

    provider = ScriptedProvider(handler)
    orchestrator, runtime, record = build_run(provider, demo_task=SAMPLE_TASK_ID)
    result = asyncio.run(orchestrator.run(record))

    assert result.record.status == "failed"
    assert result.record.changed_files == []
    assert path in (result.record.error or "")
    assert "disallowed write" in (result.record.error or "")
    # The whole change set is rejected: target.py was not applied either.
    assert "raise NotImplementedError" in runtime.workspace.read("target.py")
    # The sample tests are untouched.
    assert "test_trims_lowercases_and_dedupes" in runtime.workspace.read(
        "tests/test_target.py"
    )

    rejected = [
        event
        for event in result.events
        if event.kind == "failure"
        and event.message.startswith("Rejected a coder write")
    ]
    assert len(rejected) == 1
    assert path in rejected[0].payload["paths"]
    assert rejected[0].payload["allowed"] == ["target.py"]
    assert rejected[0].payload["read_only"] == ["tests/test_target.py"]


def test_target_py_write_is_allowed_and_verified(build_run):
    def handler(request, index):
        assert request.role != "planner"
        if request.role == "coder":
            return coder_output(
                "draft_implementation",
                [
                    {
                        "path": "target.py",
                        "content": TARGET_IMPLEMENTATION,
                        "action": "update",
                    }
                ],
            )
        return approve()

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(provider, demo_task=SAMPLE_TASK_ID)
    result = asyncio.run(orchestrator.run(record))

    assert result.record.status == "completed"
    assert result.record.changed_files == ["target.py"]
    assert result.record.verification is not None
    assert result.record.verification.tests_passed == 3


def test_demo_task_still_reviews_and_verifies(build_run):
    """A rejection is not bypassed: review runs and the run ends with findings."""
    provider = ScriptedProvider(
        lambda request, index: (
            coder_output(
                "draft_implementation",
                [
                    {
                        "path": "target.py",
                        "content": TARGET_IMPLEMENTATION,
                        "action": "update",
                    }
                ],
            )
            if request.role == "coder"
            else json.dumps(
                {
                    "verdict": "request_changes",
                    "rationale": "not convinced",
                    "findings": [
                        {
                            "severity": "minor",
                            "step_id": "draft_implementation",
                            "file": "target.py",
                            "message": "explain the dedupe",
                        }
                    ],
                }
            )
        )
    )
    orchestrator, _, record = build_run(
        provider, demo_task=SAMPLE_TASK_ID, limits=Limits(max_revisions=0, max_escalations=0)
    )
    result = asyncio.run(orchestrator.run(record))

    assert result.record.status == "completed_with_findings"
    assert result.record.review is not None
    assert result.record.review.verdict == "request_changes"
    # Review was not bypassed, and the real tests still ran diagnostically.
    assert result.record.verification is not None
    assert result.record.verification.tests_passed == 3
    assert result.record.verification.diagnostic is True