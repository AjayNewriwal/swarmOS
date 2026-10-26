"""Reproducible benchmark harness.

All policies execute the same orchestration graph. They differ only in model
selection and escalation, so a comparison measures routing cost, not a
different product:

* ``fixed-fast``   - every role uses the fast profile; escalation is off.
* ``adaptive``     - every role starts fast; one escalation to strong is allowed.
* ``multi-agent``  - role-routed profiles (planner/reviewer may use strong) with
                     escalation allowed.

The harness records only what it observed. Token and USD fields stay zero with
``usage_available``/``monetary_enforced`` false unless the provider reported
usage and rates were configured. It never declares a winner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field

from app.models import Limits, RunRequest, utc_now


@dataclass(frozen=True)
class BenchmarkPolicy:
    name: str
    model_policy: str
    max_escalations: int
    description: str


POLICIES: dict[str, BenchmarkPolicy] = {
    "fixed-fast": BenchmarkPolicy(
        "fixed-fast", "fast_only", 0, "fast profile for every role; no escalation"
    ),
    "adaptive": BenchmarkPolicy(
        "adaptive", "adaptive", 1, "fast first, one escalation to strong if configured"
    ),
    "multi-agent": BenchmarkPolicy(
        "multi-agent", "role_routed", 1, "role-routed profiles with escalation allowed"
    ),
}


class BenchmarkTask(BaseModel):
    id: str
    goal: str = Field(min_length=8)
    category: str = "coding"
    notes: str = ""


class BenchmarkResult(BaseModel):
    run_id: str
    task_id: str
    policy: str
    at: str = Field(default_factory=utc_now)
    provider: str
    provider_kind: str
    model_policy: str
    models_by_role: dict[str, str] = Field(default_factory=dict)
    status: str
    verification_passed: bool = False
    agent_calls: int = 0
    retries: int = 0
    escalations: int = 0
    revisions: int = 0
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_available: bool = False
    cost_usd: float = 0.0
    monetary_enforced: bool = False
    error: str | None = None


def load_tasks(path: Path) -> list[BenchmarkTask]:
    tasks: list[BenchmarkTask] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        tasks.append(BenchmarkTask.model_validate(json.loads(line)))
    return tasks


def result_from_run(
    *, task_id: str, policy: str, record, events: list
) -> BenchmarkResult:
    invocations = [
        event.payload.get("invocation", {})
        for event in events
        if event.kind == "agent_invocation"
    ]
    retries = sum(1 for item in invocations if int(item.get("attempt", 1) or 1) > 1)
    return BenchmarkResult(
        run_id=record.run_id,
        task_id=task_id,
        policy=policy,
        provider=record.provider,
        provider_kind=record.provider_kind,
        model_policy=record.model_policy,
        models_by_role=record.models_by_role,
        status=record.status,
        verification_passed=bool(record.verification and record.verification.passed),
        agent_calls=record.agent_calls,
        retries=retries,
        escalations=record.escalations,
        revisions=record.revisions,
        duration_ms=record.duration_ms,
        input_tokens=sum(int(item.get("input_tokens", 0) or 0) for item in invocations),
        output_tokens=sum(int(item.get("output_tokens", 0) or 0) for item in invocations),
        usage_available=record.usage_available,
        cost_usd=round(record.cost_usd, 8),
        monetary_enforced=record.monetary_enforced,
        error=record.error,
    )


def request_for(
    task: BenchmarkTask, policy: BenchmarkPolicy, provider: str, budget_usd: float
) -> RunRequest:
    return RunRequest(
        goal=task.goal,
        provider=provider,  # type: ignore[arg-type]
        model_policy=policy.model_policy,  # type: ignore[arg-type]
        limits=Limits(
            max_agent_calls=12,
            max_budget_usd=budget_usd,
            max_wall_seconds=120,
            max_retries=2,
            max_revisions=1,
            max_escalations=policy.max_escalations,
        ),
        metadata={"benchmark_task": task.id, "benchmark_policy": policy.name},
    )


def summarise(results: list[BenchmarkResult]) -> dict[str, object]:
    """Aggregate without inventing a winner.

    A cost comparison is only emitted when every result has usage metadata and
    monetary enforcement active. Otherwise the caller gets a reason instead of
    a number that would be misleading.
    """
    by_policy: dict[str, list[BenchmarkResult]] = {}
    for item in results:
        by_policy.setdefault(item.policy, []).append(item)

    aggregate = {
        name: {
            "runs": len(items),
            "completed": sum(1 for i in items if i.status == "completed"),
            "verified": sum(1 for i in items if i.verification_passed),
            "mean_agent_calls": round(sum(i.agent_calls for i in items) / len(items), 2),
            "mean_duration_ms": round(sum(i.duration_ms for i in items) / len(items), 1),
            "total_retries": sum(i.retries for i in items),
            "total_escalations": sum(i.escalations for i in items),
        }
        for name, items in by_policy.items()
    }

    comparable = bool(results) and all(
        i.usage_available and i.monetary_enforced for i in results
    )
    return {
        "policies": aggregate,
        "cost_comparable": comparable,
        "cost_note": (
            "cost comparison available"
            if comparable
            else "cost comparison withheld: usage metadata or token pricing is "
            "missing, so USD figures are estimates, not comparable measurements"
        ),
        "note": "All policies run the same orchestration graph; they differ only in "
        "model selection and escalation. No policy is declared best from this run.",
    }


def write_results(path: Path, results: list[BenchmarkResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in results:
            handle.write(item.model_dump_json())
            handle.write("\n")