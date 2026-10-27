"""Canonical demo tasks.

A demo task is a task definition, not a claim of success: it fixes the goal and
the plan so a small local model cannot turn a one-file demo into a multi-step
rewrite. Coding, review, and verification still run for real, and the trace
records that the recipe supplied the plan.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import Limits, Plan, PlanStep

SAMPLE_TASK_ID = "sample_tag_normalization"

SAMPLE_GOAL = (
    "Implement normalize_tags in target.py so the three tests in "
    "tests/test_target.py pass."
)

SAMPLE_PLAN = Plan(
    steps=[
        PlanStep(
            id="draft_implementation",
            title="Implement normalize_tags in target.py",
            detail=(
                "Implement normalize_tags in target.py. It must trim whitespace, "
                "lowercase, drop empty entries, deduplicate, and sort. Return the "
                "complete file. tests/test_target.py is read-only and must not be "
                "modified."
            ),
            acceptance_criteria=[
                "python -m pytest -q reports 3 passed",
                "tests/test_target.py is unchanged",
                "normalize_tags trims, lowercases, drops blanks, deduplicates, and sorts",
            ],
            depends_on=[],
            target_files=["target.py"],
        )
    ],
    assumptions=[
        "The workspace is the bundled sample project.",
        "The three tests in tests/test_target.py are the acceptance criteria.",
    ],
)

SAMPLE_ALLOWED_FILES = ("target.py",)
SAMPLE_READ_ONLY_FILES = ("tests/test_target.py",)
SAMPLE_LIMITS = Limits(
    max_agent_calls=6,
    max_budget_usd=0.25,
    max_wall_seconds=120,
    max_retries=0,
    max_revisions=1,
    max_escalations=0,
)


class DemoTaskError(ValueError):
    """The requested demo task is unknown or misconfigured."""


@dataclass(frozen=True)
class DemoTask:
    id: str
    goal: str
    plan: Plan
    allowed_files: tuple[str, ...]
    read_only_files: tuple[str, ...]
    limits: Limits


def _validate(task: DemoTask) -> DemoTask:
    if len(task.plan.steps) != 1:
        raise DemoTaskError(f"{task.id} must contain exactly one plan step")
    step = task.plan.steps[0]
    if the_step_files := step.target_files:
        if the_step_files != ["target.py"]:
            raise DemoTaskError(f"{task.id} must target only target.py")
    else:
        raise DemoTaskError(f"{task.id} must declare target.py")
    if task.allowed_files != ("target.py",):
        raise DemoTaskError(f"{task.id} must allow only target.py")
    if "tests/test_target.py" not in task.read_only_files:
        raise DemoTaskError(f"{task.id} must mark the tests as read-only")
    return task


SAMPLE_TASK = _validate(
    DemoTask(
        id=SAMPLE_TASK_ID,
        goal=SAMPLE_GOAL,
        plan=SAMPLE_PLAN,
        allowed_files=SAMPLE_ALLOWED_FILES,
        read_only_files=SAMPLE_READ_ONLY_FILES,
        limits=SAMPLE_LIMITS,
    )
)

_DEMO_TASKS = {SAMPLE_TASK_ID: SAMPLE_TASK}


def resolve_demo_task(task_id: str) -> DemoTask:
    try:
        return _DEMO_TASKS[task_id]
    except KeyError as exc:
        raise DemoTaskError(f"unknown demo task: {task_id!r}") from exc