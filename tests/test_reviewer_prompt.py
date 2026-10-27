"""Focused tests for the Review Agent instructions.

They pin the evidence requirements that failed in run_478959ca3d86: the reviewer
rejected correct code by restating the acceptance criteria without citing a line
or showing a failing input.
"""

from __future__ import annotations

import json

from app.agents.reviewer import SYSTEM, ReviewAgent
from app.models import FileChange, Plan, PlanStep, ReviewInput, ReviewOutput

ACCEPTANCE = "normalize_tags trims, lowercases, drops blanks, deduplicates, and sorts"


def sample_plan() -> Plan:
    return Plan(
        steps=[
            PlanStep(
                id="draft_implementation",
                title="Implement normalize_tags",
                detail="Implement normalize_tags in target.py",
                acceptance_criteria=[ACCEPTANCE],
                target_files=["target.py"],
            )
        ]
    )


def review_input() -> ReviewInput:
    return ReviewInput(
        goal="Implement tag normalization so the failing tests pass",
        plan=sample_plan(),
        changes=[FileChange(path="target.py", content="x = 1\n", step_id="draft_implementation")],
        files={
            "target.py": "x = 1\n",
            "tests/test_target.py": "def test_ok():\n    assert True\n",
        },
    )


def _normalised(text: str) -> str:
    return " ".join(text.split())


def test_prompt_requires_evidence_for_every_finding():
    prompt = _normalised(SYSTEM)
    assert "line number or exact code expression" in prompt
    assert "acceptance criterion" in prompt
    assert "expected versus actual behaviour" in prompt
    assert "why that evidence demonstrates a real defect" in prompt


def test_prompt_forbids_rejection_by_restating_criteria():
    prompt = _normalised(SYSTEM)
    assert "Never reject work by restating an acceptance criterion" in prompt
    assert "is not a finding unless you can point at the line" in prompt


def test_prompt_forbids_unsubstantiated_blockers_and_invented_requirements():
    prompt = _normalised(SYSTEM)
    assert "do not report it as a blocker" in prompt
    assert "Do not invent requirements" in prompt


def test_prompt_keeps_genuine_blockers_forcing_changes():
    prompt = _normalised(SYSTEM)
    assert "Any blocker finding forces a request_changes verdict" in prompt
    assert "Approve only when you have no substantiated blocking defect" in prompt


def test_prompt_still_asks_for_the_typed_json_shape():
    prompt = _normalised(SYSTEM)
    assert '"verdict": "approve" | "request_changes"' in prompt
    assert '"severity": "blocker" | "major" | "minor" | "nit"' in prompt


def test_review_request_points_the_reviewer_at_the_tests():
    request = ReviewAgent().build_request(review_input(), "test-model")
    payload = json.loads(request.user)

    assert "tests" in payload["note"]
    assert "acceptance criteria" in payload["note"]
    # The tests and the acceptance criteria are actually present in the payload.
    assert "tests/test_target.py" in payload["files"]
    assert payload["plan"]["steps"][0]["acceptance_criteria"] == [ACCEPTANCE]


def test_blocker_still_forces_request_changes():
    """The prompt change must not weaken the deterministic blocker rule."""
    review = ReviewOutput(
        verdict="approve",
        rationale="looks fine",
        findings=[
            {
                "severity": "blocker",
                "step_id": "draft_implementation",
                "file": "target.py",
                "message": "line 5 returns an unsorted list",
            }
        ],
    )
    assert review.verdict == "request_changes"