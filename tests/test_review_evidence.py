"""Focused tests for the reviewer evidence-shape policy.

A finding with thin evidence is still a finding: the rejection stands, the run
keeps its request_changes verdict, and it ends as ``completed_with_findings``.
Only a rejection that names no finding is malformed output, and even then the
run is never approved.
"""

from __future__ import annotations

import json

from app.models import Limits, ReviewFinding, ReviewOutput
from app.review_evidence import validate_review
from tests.conftest import SEED_OK, ScriptedProvider, coder_response, plan_response

PLAN = {
    "steps": [
        {
            "id": "alpha",
            "title": "Create alpha",
            "detail": "Write out/alpha.txt",
            "acceptance_criteria": ["out/alpha.txt exists"],
            "depends_on": [],
            "target_files": ["out/alpha.txt"],
        }
    ]
}

THIN_BLOCKER = ReviewFinding(
    severity="blocker",
    step_id="alpha",
    file="out/alpha.txt",
    message="alpha is wrong.",
)


def test_thin_blocker_is_a_rejection_with_gaps_not_malformed():
    report = validate_review(
        ReviewOutput(verdict="request_changes", findings=[THIN_BLOCKER]),
        acceptance_criteria=["out/alpha.txt exists"],
        files={"out/alpha.txt": "ok\n"},
    )

    assert report.is_malformed is False
    assert len(report.unsupported_blockers) == 1
    assert "code reference or line" in report.unsupported_blockers[0].missing


def test_well_evidenced_blocker_has_no_gaps():
    finding = ReviewFinding(
        severity="blocker",
        step_id="alpha",
        file="out/alpha.txt",
        message=(
            'out/alpha.txt line 2 raises, so "out/alpha.txt exists" fails: '
            "expected content 'ok', actual missing."
        ),
    )
    report = validate_review(
        ReviewOutput(verdict="request_changes", findings=[finding]),
        acceptance_criteria=["out/alpha.txt exists"],
        files={"out/alpha.txt": "ok\n"},
    )

    assert report.is_malformed is False
    assert report.unsupported_blockers == ()
    assert report.supported_findings == report.checks


async def test_rejection_without_findings_is_not_an_approval(build_run):
    def handler(request, index):
        if request.role == "planner":
            return plan_response(PLAN)
        if request.role == "coder":
            return coder_response("alpha")
        return json.dumps(
            {"verdict": "request_changes", "rationale": "nope", "findings": []}
        )

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(
        provider,
        files=SEED_OK,
        limits=Limits(max_revisions=1, max_escalations=0),
    )
    result = await orchestrator.run(record)

    # Malformed output: reported as an invalid review, never as approval.
    assert result.record.status == "review_invalid"
    assert result.record.review is not None
    assert result.record.review.verdict == "request_changes"
    # Nothing was dispatched and no review round was spent on it.
    assert result.record.revisions == 0
    assert len([call for call in provider.calls if call.role == "coder"]) == 1
    assert len([call for call in provider.calls if call.role == "reviewer"]) == 1
