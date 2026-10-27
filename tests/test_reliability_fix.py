"""Regression tests for the reliability fix behind run_37204d26ec8b.

Three behaviours are covered:

1. a run rejected by the reviewer now carries diagnostic test evidence, without
   changing the status or re-entering the coding/review loop;
2. ``revisions`` counts dispatched revision rounds, not review rejections;
3. agent calls leave a concise, secret-free output summary in the trace.
"""

from __future__ import annotations

import json

import pytest

from app.models import Limits, RunRecord, TraceEvent
from app.store import RunStore
from tests.conftest import (
    SEED_OK,
    TARGET_IMPLEMENTATION,
    ScriptedProvider,
    plan_response,
)

SINGLE_STEP_PLAN = {
    "steps": [
        {
            "id": "fix_target",
            "title": "Fix target.py",
            "detail": "Implement normalize_tags in target.py",
            "acceptance_criteria": ["tests/test_target.py passes"],
            "depends_on": [],
            "target_files": ["target.py"],
        }
    ]
}

BROKEN_TARGET = (
    "from collections.abc import Iterable\n\n\n"
    "def normalize_tags(tags: Iterable[str]) -> list[str]:\n"
    '    """Stub."""\n'
    "    raise NotImplementedError\n"
)

# The exact file retained in run_37204d26ec8b: trims, lower-cases and drops
# blanks, but does not de-duplicate or sort.
RUN_37204_ARTIFACT = (
    "from collections.abc import Iterable\n\n\n"
    "def normalize_tags(tags: Iterable[str]) -> list[str]:\n"
    '    """Return normalized tags.\n\n'
    "    Expected behaviour is described by ``tests/test_target.py``.\n"
    '    """\n'
    "    return [tag.strip().lower() for tag in tags if tag.strip()]\n"
)


def coder_writing(content: str, summary: str = "Rewrote normalize_tags"):
    def handler(request, index):
        if request.role == "planner":
            return plan_response(SINGLE_STEP_PLAN)
        if request.role == "coder":
            return json.dumps(
                {
                    "step_id": "fix_target",
                    "summary": summary,
                    "rationale": "Matches the acceptance criterion.",
                    "changes": [
                        {"path": "target.py", "content": content, "action": "update"}
                    ],
                }
            )
        return json.dumps(
            {
                "verdict": "request_changes",
                "rationale": "Still does not satisfy the acceptance criteria.",
                "findings": [
                    {
                        "severity": "blocker",
                        "step_id": "fix_target",
                        "file": "target.py",
                        "message": "normalize_tags does not match the expected behaviour.",
                    }
                ],
            }
        )

    return handler


def events_of(result, kind: str) -> list:
    return [event for event in result.events if event.kind == kind]


# ----------------------------------------------------------------------
# 1. diagnostic verification
# ----------------------------------------------------------------------
async def test_rejected_run_records_diagnostic_verification(build_run):
    provider = ScriptedProvider(coder_writing(BROKEN_TARGET))
    orchestrator, _, record = build_run(
        provider,
        use_sample=True,
        limits=Limits(max_revisions=0, max_escalations=0),
    )
    result = await orchestrator.run(record)

    # The status must not be upgraded by the diagnostic run.
    assert result.record.status == "completed_with_findings"
    assert result.record.review is not None
    assert result.record.review.verdict == "request_changes"

    # Real evidence is attached.
    assert result.record.verification is not None
    assert result.record.verification.passed is False
    assert result.record.verification.exit_code != 0
    assert result.record.verification.tests_failed >= 1

    # The trace shows it was diagnostic.
    verifications = events_of(result, "verification")
    assert len(verifications) == 1
    assert verifications[0].payload["diagnostic"] is True
    assert verifications[0].payload["passed"] is False

    finished = events_of(result, "run_finished")[0]
    assert finished.payload["verification"]["diagnostic"] is True
    assert finished.payload["status"] == "completed_with_findings"

    assert "Verification (diagnostic): failed" in result.record.summary
    assert "does not change the review verdict" in result.record.summary


async def test_diagnostic_verification_passing_does_not_approve_the_run(build_run):
    provider = ScriptedProvider(coder_writing(TARGET_IMPLEMENTATION))
    orchestrator, _, record = build_run(
        provider,
        use_sample=True,
        limits=Limits(max_revisions=0, max_escalations=0),
    )
    result = await orchestrator.run(record)

    # The tests pass, but the reviewer rejected the work, so the verdict stands.
    assert result.record.verification is not None
    assert result.record.verification.passed is True
    assert result.record.status == "completed_with_findings"
    assert result.record.review is not None
    assert result.record.review.verdict == "request_changes"
    assert "Verification (diagnostic): passed" in result.record.summary


async def test_run_37204_artifact_now_reports_its_real_failures(build_run):
    """Replay the file from the incident and prove the evidence is attached.

    The reviewer's second finding claimed the empty-list case was broken. The
    diagnostic run shows the opposite: the empty-list test passes, while
    de-duplication and sorting fail.
    """
    provider = ScriptedProvider(coder_writing(RUN_37204_ARTIFACT))
    orchestrator, _, record = build_run(
        provider,
        use_sample=True,
        limits=Limits(max_revisions=0, max_escalations=0),
    )
    result = await orchestrator.run(record)

    verification = result.record.verification
    assert verification is not None
    assert verification.diagnostic is True
    assert verification.passed is False
    assert verification.tests_passed == 1
    assert verification.tests_failed == 2
    assert result.record.status == "completed_with_findings"


async def test_diagnostic_verification_timeout_is_recorded_safely(build_run):
    slow_test = {
        "tests/test_slow.py": "import time\n\n\ndef test_slow():\n    time.sleep(30)\n"
    }
    provider = ScriptedProvider(coder_writing(BROKEN_TARGET))
    orchestrator, _, record = build_run(
        provider,
        files=slow_test,
        limits=Limits(max_revisions=0, max_escalations=0),
        verification_timeout_seconds=1,
    )
    result = await orchestrator.run(record)

    assert result.record.status == "completed_with_findings"
    assert result.record.verification is not None
    assert result.record.verification.passed is False
    assert result.record.verification.exit_code == 124
    assert "timed out" in result.record.verification.stderr_tail
    assert "Verification (diagnostic): failed" in result.record.summary


async def test_diagnostic_verification_records_why_it_could_not_run(build_run):
    provider = ScriptedProvider(coder_writing(BROKEN_TARGET))
    orchestrator, _, record = build_run(
        provider,
        use_sample=True,
        limits=Limits(max_revisions=0, max_escalations=0),
        execution_enabled=False,
    )
    result = await orchestrator.run(record)

    assert result.record.status == "completed_with_findings"
    assert result.record.verification is not None
    assert result.record.verification.passed is False
    assert result.record.verification.exit_code == 126
    assert "disabled" in result.record.verification.stderr_tail


async def test_diagnostic_verification_does_not_start_new_rounds(build_run):
    provider = ScriptedProvider(coder_writing(BROKEN_TARGET))
    orchestrator, _, record = build_run(
        provider,
        use_sample=True,
        limits=Limits(max_revisions=1, max_escalations=0),
    )
    result = await orchestrator.run(record)

    coders = [call for call in provider.calls if call.role == "coder"]
    reviewers = [call for call in provider.calls if call.role == "reviewer"]
    assert len(coders) == 2  # initial + one revision round
    assert len(reviewers) == 2  # initial review + review after the revision
    # The failed diagnostic must not trigger a repair round.
    assert result.record.revisions == 1
    assert result.record.status == "completed_with_findings"
    repairs = [
        event
        for event in events_of(result, "routing_decision")
        if event.payload.get("mode") == "repair"
    ]
    assert repairs == []


# ----------------------------------------------------------------------
# 2. revision accounting
# ----------------------------------------------------------------------
@pytest.mark.parametrize("max_revisions", [0, 1, 2])
async def test_revision_count_is_dispatched_rounds_not_rejections(build_run, max_revisions):
    provider = ScriptedProvider(coder_writing(BROKEN_TARGET))
    orchestrator, _, record = build_run(
        provider,
        use_sample=True,
        limits=Limits(max_revisions=max_revisions, max_escalations=0),
    )
    result = await orchestrator.run(record)

    reviewers = [call for call in provider.calls if call.role == "reviewer"]
    coders = [call for call in provider.calls if call.role == "coder"]

    # One rejection per review, but only max_revisions revision rounds.
    assert len(reviewers) == max_revisions + 1
    assert result.record.revisions == max_revisions
    assert len(coders) == max_revisions + 1

    rejections = events_of(result, "revision")
    assert len(rejections) == max_revisions + 1
    assert [event.payload["review_rejection"] for event in rejections] == list(
        range(1, max_revisions + 2)
    )

    dispatches = [
        event
        for event in events_of(result, "routing_decision")
        if event.payload.get("mode") == "revision"
    ]
    assert len(dispatches) == max_revisions


async def test_revision_rounds_are_counted_once_per_dispatch_not_per_branch(build_run):
    """A wave of two independent revised steps is one revision round."""
    from tests.conftest import PLAN_THREE_STEPS

    def handler(request, index):
        if request.role == "planner":
            return plan_response(PLAN_THREE_STEPS)
        if request.role == "coder":
            step_id = json.loads(request.user)["step"]["id"]
            return json.dumps(
                {
                    "step_id": step_id,
                    "summary": f"wrote {step_id}",
                    "rationale": "r",
                    "changes": [
                        {"path": f"out/{step_id}.txt", "content": "x", "action": "create"}
                    ],
                }
            )
        return json.dumps(
            {
                "verdict": "request_changes",
                "rationale": "nope",
                "findings": [
                    {"severity": "major", "step_id": "alpha", "message": "redo alpha"}
                ],
            }
        )

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(
        provider,
        files=SEED_OK,
        limits=Limits(max_revisions=1, max_escalations=0),
    )
    result = await orchestrator.run(record)

    # One revision round even though the reviewer flagged a single step.
    assert result.record.revisions == 1


# ----------------------------------------------------------------------
# 3. auditability
# ----------------------------------------------------------------------
async def test_agent_output_summaries_are_persisted(build_run):
    provider = ScriptedProvider(coder_writing(BROKEN_TARGET))
    orchestrator, _, record = build_run(
        provider,
        use_sample=True,
        limits=Limits(max_revisions=0, max_escalations=0),
    )
    result = await orchestrator.run(record)

    invocations = events_of(result, "agent_invocation")
    by_role = {event.payload["role"]: event for event in invocations}

    planner = by_role["planner"].payload["output_summary"]
    assert planner["kind"] == "plan"
    assert planner["steps"] == ["fix_target"]

    coder = by_role["coder"].payload["output_summary"]
    assert coder["kind"] == "coding"
    assert coder["step_id"] == "fix_target"
    assert "Rewrote normalize_tags" in coder["summary"]
    assert coder["changes"] == ["target.py"]
    assert coder["change_count"] == 1

    reviewer = by_role["reviewer"].payload["output_summary"]
    assert reviewer["kind"] == "review"
    assert reviewer["verdict"] == "request_changes"
    assert reviewer["finding_count"] == 1
    assert "does not match" in reviewer["findings"][0]["message"]


async def test_agent_summaries_are_truncated_and_carry_no_prompts(build_run):
    # 2000 chars is the maximum the CodingOutput schema accepts; the persisted
    # summary is clipped further so a trace never becomes a transcript.
    long_text = "x" * 2000
    provider = ScriptedProvider(coder_writing(BROKEN_TARGET, summary=long_text))
    orchestrator, _, record = build_run(
        provider,
        use_sample=True,
        limits=Limits(max_revisions=0, max_escalations=0),
        api_key="sk-test-secret-value-000",
    )
    result = await orchestrator.run(record)

    for event in events_of(result, "agent_invocation"):
        summary = event.payload["output_summary"]
        assert summary, "every successful invocation keeps a summary"
        for value in summary.values():
            if isinstance(value, str):
                assert len(value) <= 320
        # No prompt material and no credentials are persisted.
        blob = json.dumps(event.payload)
        assert "You are the Coding Agent" not in blob
        assert "sk-test-secret-value-000" not in blob
        assert "system" not in event.payload
        assert "user" not in event.payload
        assert "prompt" not in event.payload


async def test_legacy_trace_events_without_summaries_remain_readable(tmp_path):
    store = RunStore(tmp_path / "legacy.db")
    try:
        record_payload = {
            "run_id": "run_legacy",
            "goal": "legacy goal",
            "status": "completed",
            "limits": Limits().model_dump(),
        }
        store.save_run(RunRecord(**record_payload))
        legacy = TraceEvent(
            run_id="run_legacy",
            seq=1,
            kind="agent_invocation",
            message="coder succeeded",
            payload={"role": "coder", "status": "succeeded", "model": "old-model"},
        )
        store.append_event(legacy)

        events = store.get_events("run_legacy")
        assert len(events) == 1
        assert "output_summary" not in events[0].payload
        assert events[0].payload["role"] == "coder"
    finally:
        store.close()

async def test_demo_run_does_not_claim_configured_real_models(build_run):
    from app.providers.demo import DemoProvider

    orchestrator, _, record = build_run(
        DemoProvider(),
        use_sample=True,
        limits=Limits(),
        base_url="http://127.0.0.1:11434/v1",
        fast_model="qwen2.5-coder:1.5b",
    )
    result = await orchestrator.run(record)

    assert result.record.provider == "demo"
    assert result.record.models_by_role["planner"] == "demo-fast"
    assert result.record.models_by_role["coder"] == "demo-fast"
    assert "planner=demo-fast" in result.record.summary
    assert "qwen2.5-coder:1.5b" not in result.record.summary
