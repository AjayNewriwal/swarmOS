from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import FileChange, Limits, Plan, PlanStep, ReviewFinding, ReviewOutput


def step(step_id: str, depends_on: list[str] | None = None) -> PlanStep:
    return PlanStep(
        id=step_id,
        title=f"Step {step_id}",
        detail="do the thing",
        acceptance_criteria=["it works"],
        depends_on=depends_on or [],
    )


def test_plan_rejects_cycles():
    with pytest.raises(ValidationError, match="cycle"):
        Plan(steps=[step("a", ["b"]), step("b", ["a"])])


def test_plan_rejects_unknown_dependency():
    with pytest.raises(ValidationError, match="unknown step"):
        Plan(steps=[step("a", ["ghost"])])


def test_plan_rejects_duplicate_ids():
    with pytest.raises(ValidationError, match="unique"):
        Plan(steps=[step("a"), step("a")])


def test_plan_rejects_self_dependency():
    with pytest.raises(ValidationError, match="itself"):
        Plan(steps=[step("a", ["a"])])


def test_waves_group_independent_steps():
    plan = Plan(
        steps=[
            step("a"),
            step("b"),
            step("c", ["a"]),
            step("d", ["a", "b"]),
        ]
    )
    waves = [[item.id for item in wave] for wave in plan.waves()]
    assert waves == [["a", "b"], ["c", "d"]]


def test_step_id_must_be_snake_case():
    with pytest.raises(ValidationError):
        step("Bad-Id")


def test_file_change_rejects_traversal_and_absolute_paths():
    for path in ["../escape.py", "/etc/passwd", r"C:\windows\system32", "a/../b.py"]:
        with pytest.raises(ValidationError):
            FileChange(path=path, content="x")


def test_blocker_forces_request_changes_verdict():
    review = ReviewOutput(
        verdict="approve",
        findings=[ReviewFinding(severity="blocker", message="this is broken")],
    )
    assert review.verdict == "request_changes"


def test_limits_reject_negative_and_excessive_values():
    with pytest.raises(ValidationError):
        Limits(max_budget_usd=-1)
    with pytest.raises(ValidationError):
        Limits(max_agent_calls=1000)


def test_empty_acceptance_criteria_rejected():
    with pytest.raises(ValidationError):
        PlanStep(
            id="a",
            title="Step a",
            detail="detail",
            acceptance_criteria=["   "],
        )