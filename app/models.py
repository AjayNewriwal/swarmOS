"""Typed contracts shared by the API, the graph, and the storage layer.

These models are the boundary between agents: every agent reads a typed input
model and must return a typed output model, so routing and failure handling can
reason about data instead of parsing prose.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

AgentRole = Literal["planner", "coder", "reviewer", "verifier"]
ModelTier = Literal["fast", "strong"]
RunStatus = Literal[
    "queued",
    "running",
    "completed",
    "completed_with_findings",
    "review_invalid",
    "failed",
    "budget_exceeded",
    "limit_exceeded",
]

_STEP_ID = re.compile(r"^[a-z][a-z0-9_]{0,39}$")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Limits(BaseModel):
    """Autonomy envelope for a single run.

    Values are validated against the process ceiling by ``clamp_limits`` before
    a run starts; a caller can tighten a limit but not widen it.
    """

    max_agent_calls: int = Field(default=12, ge=1, le=64)
    max_budget_usd: float = Field(default=0.25, ge=0, le=20)
    max_wall_seconds: int = Field(default=120, ge=5, le=1800)
    max_retries: int = Field(default=2, ge=0, le=5)
    max_revisions: int = Field(default=1, ge=0, le=3)
    max_escalations: int = Field(default=1, ge=0, le=2)
    parallel_coding: bool = True


class RunRequest(BaseModel):
    goal: str = Field(min_length=8, max_length=5000)
    files: dict[str, str] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)
    provider: Literal["auto", "demo", "openai"] = "auto"
    model_policy: Literal["config", "fast_only", "adaptive", "role_routed"] | None = None
    demo_task: Literal["sample_tag_normalization"] | None = None
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("files")
    @classmethod
    def _validate_files(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 50:
            raise ValueError("at most 50 seed files are allowed")
        for path, content in value.items():
            safe_relative_path(path)
            if len(content) > 200_000:
                raise ValueError(f"seed file {path!r} exceeds 200k characters")
        return value


def safe_relative_path(path: str) -> str:
    raw = path.replace("\\", "/").strip()
    if not raw:
        raise ValueError("file path must not be empty")
    if raw.startswith("/") or raw.startswith("~") or re.match(r"^[a-zA-Z]:", raw):
        raise ValueError(f"absolute paths are not allowed: {path!r}")
    normalized = raw.strip("/")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe file path: {path!r}")
    if len(normalized) > 240:
        raise ValueError("file path is too long")
    return normalized


class PlanStep(BaseModel):
    id: str
    title: str = Field(min_length=3, max_length=120)
    detail: str = Field(min_length=3, max_length=2000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=8)
    depends_on: list[str] = Field(default_factory=list)
    target_files: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        if not _STEP_ID.match(value):
            raise ValueError("step id must match ^[a-z][a-z0-9_]{0,39}$")
        return value

    @field_validator("depends_on")
    @classmethod
    def _validate_dep_ids(cls, value: list[str]) -> list[str]:
        for dep in value:
            if not _STEP_ID.match(dep):
                raise ValueError(f"invalid dependency id: {dep!r}")
        return list(dict.fromkeys(value))

    @field_validator("target_files")
    @classmethod
    def _validate_target_files(cls, value: list[str]) -> list[str]:
        return [safe_relative_path(path) for path in value]

    @field_validator("acceptance_criteria")
    @classmethod
    def _validate_criteria(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        if not cleaned:
            raise ValueError("at least one non-empty acceptance criterion is required")
        return cleaned


class Plan(BaseModel):
    steps: list[PlanStep] = Field(min_length=1, max_length=12)
    assumptions: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_graph(self) -> Plan:
        ids = [step.id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("plan step ids must be unique")
        known = set(ids)
        for step in self.steps:
            for dep in step.depends_on:
                if dep == step.id:
                    raise ValueError(f"step {step.id!r} cannot depend on itself")
                if dep not in known:
                    raise ValueError(f"step {step.id!r} depends on unknown step {dep!r}")
        # Kahn's algorithm: reject cycles so the wave scheduler always terminates.
        indegree = {step.id: len(step.depends_on) for step in self.steps}
        children: dict[str, list[str]] = {step.id: [] for step in self.steps}
        for step in self.steps:
            for dep in step.depends_on:
                children[dep].append(step.id)
        ready = [step_id for step_id, degree in indegree.items() if degree == 0]
        seen = 0
        while ready:
            current = ready.pop()
            seen += 1
            for child in children[current]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if seen != len(self.steps):
            raise ValueError("plan contains a dependency cycle")
        return self

    def step(self, step_id: str) -> PlanStep:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise KeyError(step_id)

    def waves(self) -> list[list[PlanStep]]:
        """Topological levels: steps inside one wave are mutually independent."""
        remaining = {step.id: set(step.depends_on) for step in self.steps}
        order = {step.id: index for index, step in enumerate(self.steps)}
        levels: list[list[PlanStep]] = []
        resolved: set[str] = set()
        while remaining:
            ready = [step_id for step_id, deps in remaining.items() if deps <= resolved]
            if not ready:
                raise ValueError("plan contains a dependency cycle")
            ready.sort(key=lambda step_id: order[step_id])
            levels.append([self.step(step_id) for step_id in ready])
            for step_id in ready:
                del remaining[step_id]
                resolved.add(step_id)
        return levels


class FileChange(BaseModel):
    path: str
    content: str = ""
    action: Literal["create", "update", "delete"] = "create"
    step_id: str | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return safe_relative_path(value)


class PlannerInput(BaseModel):
    goal: str = Field(min_length=8, max_length=5000)


class CodingInput(BaseModel):
    goal: str
    step: PlanStep
    files: dict[str, str] = Field(default_factory=dict)
    review_findings: list[ReviewFinding] = Field(default_factory=list)
    verification_stdout: str = ""
    # Request-scoped write restrictions. Empty lists mean the normal behavior.
    allowed_files: list[str] = Field(default_factory=list)
    read_only_files: list[str] = Field(default_factory=list)


class CodingOutput(BaseModel):
    step_id: str
    summary: str = Field(min_length=3, max_length=2000)
    rationale: str = Field(default="", max_length=4000)
    changes: list[FileChange] = Field(default_factory=list)


class ReviewFinding(BaseModel):
    severity: Literal["blocker", "major", "minor", "nit"] = "major"
    step_id: str | None = None
    file: str | None = None
    message: str = Field(min_length=3, max_length=2000)


class ReviewInput(BaseModel):
    goal: str
    plan: Plan
    changes: list[FileChange] = Field(default_factory=list)
    files: dict[str, str] = Field(default_factory=dict)


class ReviewOutput(BaseModel):
    verdict: Literal["approve", "request_changes"]
    rationale: str = Field(default="", max_length=4000)
    findings: list[ReviewFinding] = Field(default_factory=list)

    @model_validator(mode="after")
    def _blockers_mean_changes(self) -> ReviewOutput:
        if any(f.severity == "blocker" for f in self.findings):
            self.verdict = "request_changes"
        return self


class VerificationResult(BaseModel):
    passed: bool
    command: str
    exit_code: int
    duration_ms: int
    tests_passed: int = 0
    tests_failed: int = 0
    stdout_tail: str = ""
    stderr_tail: str = ""
    workspace_files: list[str] = Field(default_factory=list)
    # True when this ran after a rejected review, purely for evidence.
    diagnostic: bool = False


class AgentInvocation(BaseModel):
    invocation_id: str = Field(default_factory=lambda: new_id("inv"))
    run_id: str
    role: AgentRole
    step_id: str | None = None
    provider: str
    model: str
    profile: str = "fast"
    model_tier: ModelTier = "fast"
    attempt: int = 1
    status: Literal["succeeded", "failed", "skipped"] = "succeeded"
    started_at: str = Field(default_factory=utc_now)
    finished_at: str | None = None
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_available: bool = False
    structured_mode: str | None = None
    cost_usd: float = 0.0
    error: str | None = None
    error_class: str | None = None


TraceKind = Literal[
    "run_started",
    "agent_invocation",
    "routing_decision",
    "verification",
    "failure",
    "retry",
    "escalation",
    "revision",
    "budget",
    "provider",
    "repair",
    "run_finished",
]


class TraceEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("evt"))
    run_id: str
    seq: int = 0
    at: str = Field(default_factory=utc_now)
    kind: TraceKind
    message: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class RunRecord(BaseModel):
    run_id: str = Field(default_factory=lambda: new_id("run"))
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)
    goal: str
    provider: str = "demo"
    provider_kind: Literal["demo", "local", "remote", "unknown"] = "demo"
    provider_warning: str | None = None
    status: RunStatus = "queued"
    limits: Limits = Field(default_factory=Limits)
    model_policy: str = "config"
    models_by_role: dict[str, str] = Field(default_factory=dict)
    plan: Plan | None = None
    review: ReviewOutput | None = None
    verification: VerificationResult | None = None
    summary: str = ""
    error: str | None = None
    cost_usd: float = 0.0
    monetary_enforced: bool = False
    usage_available: bool = False
    agent_calls: int = 0
    escalations: int = 0
    # Revision rounds actually dispatched, not the number of review rejections.
    revisions: int = 0
    duration_ms: int = 0
    changed_files: list[str] = Field(default_factory=list)
    artifacts: dict[str, str] = Field(default_factory=dict)


class RunDetail(BaseModel):
    run: RunRecord
    events: list[TraceEvent] = Field(default_factory=list)


CodingInput.model_rebuild()