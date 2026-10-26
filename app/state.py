"""The explicit shared state for the orchestration graph.

Every field the agents coordinate through is declared here. Parallel branches
may only write channels that carry a reducer, which makes concurrent execution
safe by construction instead of by convention.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel

from app.models import (
    AgentInvocation,
    FileChange,
    ModelTier,
    Plan,
    ReviewOutput,
    RunStatus,
    VerificationResult,
)


class StepFailure(BaseModel):
    step_id: str
    stage: Literal["coding", "review", "verification"]
    error: str
    attempts: int = 1


class RunHalt(BaseModel):
    """A reduced channel so parallel branches can all request a stop safely."""

    status: Literal["budget_exceeded", "limit_exceeded"]
    error: str


def merge_lists(left: list | None, right: list | None) -> list:
    return list(left or []) + list(right or [])


def merge_dict_max(left: dict[str, int] | None, right: dict[str, int] | None) -> dict[str, int]:
    merged = dict(left or {})
    for key, value in (right or {}).items():
        merged[key] = max(merged.get(key, 0), value)
    return merged


def merge_max_int(left: int | None, right: int | None) -> int:
    """Keep the highest revision-round number reported by a coding branch."""
    return max(int(left or 0), int(right or 0))


class SwarmState(TypedDict, total=False):
    # --- identity and envelope (written by the service, read-only in nodes)
    run_id: str
    goal: str
    limits: dict
    provider: str
    provider_warning: str | None
    started_at: float
    started_wall: str

    # --- planning
    plan: Plan | None
    model_tier: ModelTier

    # --- workspace (single-writer: the advance node)
    workspace: dict[str, str]

    # --- parallel-safe accumulators
    changes: Annotated[list[FileChange], merge_lists]
    completed_steps: list[str]
    coded_steps: Annotated[list[str], merge_lists]
    failures: Annotated[list[StepFailure], merge_lists]
    agent_invocations: Annotated[list[AgentInvocation], merge_lists]
    cost_usd: Annotated[float, operator.add]
    agent_calls: Annotated[int, operator.add]
    attempts: Annotated[dict[str, int], merge_dict_max]
    halt: Annotated[list[RunHalt], merge_lists]
    merged_changes: int
    merged_coded: int

    # --- review / verification (single-writer)
    review: ReviewOutput | None
    pending_review_steps: list[str]
    # Routing counter: how many review verdicts requested changes. It drives the
    # ``max_revisions`` limit and is not the number reported as revisions.
    review_rejections: int
    # Reported counter: highest revision round dispatched. A wave of independent
    # steps counts as one round, so parallel branches report the same number.
    revisions: Annotated[int, merge_max_int]
    verification: VerificationResult | None
    verification_attempts: int
    escalations: int
    stage: Literal["planning", "coding", "review", "verification", "done"]

    # --- outcome
    status: RunStatus
    error: str | None
    summary: str


def initial_state(
    *,
    run_id: str,
    goal: str,
    limits: dict,
    provider: str,
    provider_warning: str | None,
    started_at: float,
    started_wall: str,
) -> SwarmState:
    return SwarmState(
        run_id=run_id,
        goal=goal,
        limits=limits,
        provider=provider,
        provider_warning=provider_warning,
        started_at=started_at,
        started_wall=started_wall,
        plan=None,
        model_tier="fast",
        workspace={},
        changes=[],
        completed_steps=[],
        coded_steps=[],
        failures=[],
        agent_invocations=[],
        cost_usd=0.0,
        agent_calls=0,
        attempts={},
        halt=[],
        merged_changes=0,
        merged_coded=0,
        review=None,
        pending_review_steps=[],
        review_rejections=0,
        revisions=0,
        verification=None,
        verification_attempts=0,
        escalations=0,
        stage="planning",
        status="running",
        error=None,
        summary="",
    )


class CodeStepPayload(TypedDict):
    """The per-branch state handed to a coding node by ``Send``."""

    run_id: str
    goal: str
    step: dict
    model_tier: ModelTier
    attempt: int
    files: dict[str, str]
    review_findings: list[dict]
    verification_stdout: str
    workspace_subdir: str
    # initial | retry | revision | repair
    mode: str
    # For mode == "revision": the review rejection that triggered this round.
    revision_round: int