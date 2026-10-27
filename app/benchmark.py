"""Reproducible benchmark harness.

All policies execute the same orchestration graph. They differ only in model
selection and escalation, so a comparison measures routing cost, not a
different product:

* ``fixed-fast``   - every role uses the fast profile; escalation is off.
* ``adaptive``     - every role starts fast; one escalation to strong is allowed.
* ``multi-agent``  - role-routed profiles (planner/reviewer may use strong) with
                     escalation allowed.

The harness records only what it observed. A metric is ``None`` (unavailable)
when the provider did not report it: unreported token counts are not zero, and a
USD figure is withheld unless usage is measured *and* pricing is configured. It
never declares a winner.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from app.models import Limits, RunRequest, utc_now

SUCCESS_STATUSES = {"completed", "completed_with_findings"}
LIMIT_STATUSES = {"budget_exceeded", "limit_exceeded"}


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

# Stable ordering for reports and comparisons.
POLICY_ORDER = ("fixed-fast", "adaptive", "multi-agent")


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
    simulated: bool = False
    model_policy: str
    models_by_role: dict[str, str] = Field(default_factory=dict)
    # Distinct model ids actually invoked, per role. On an escalated run this
    # shows both profiles, which models_by_role (the starting selection) cannot.
    models_used: dict[str, list[str]] = Field(default_factory=dict)
    escalation_triggers: list[str] = Field(default_factory=list)
    escalation_available: bool | None = None
    status: str
    review_verdict: str | None = None
    verification_passed: bool = False
    verification_attempts: int = 0
    verification_diagnostic: bool = False
    # True when the recorded verification gated the outcome (not diagnostic).
    verification_gating: bool = False
    verification_exit_code: int | None = None
    agent_calls: int = 0
    planner_calls: int = 0
    coder_calls: int = 0
    reviewer_calls: int = 0
    retries: int = 0
    escalations: int = 0
    revisions: int = 0
    reviewer_rejections: int = 0
    invalid_reviews: int = 0
    evidence_gap_events: int = 0
    limit_exhausted: bool = False
    denied_calls: int = 0
    duration_ms: int = 0
    # ``None`` means the provider did not report usage, so the value is unknown.
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_available: bool = False
    # ``None`` means cost is not measurable (no usage, or no configured pricing).
    cost_usd: float | None = None
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


def _event_payloads(events: list, kind: str) -> list[dict[str, Any]]:
    return [event.payload for event in events if event.kind == kind]


def result_from_run(
    *,
    task_id: str,
    policy: str,
    record,
    events: list,
    escalation_available: bool | None = None,
    simulated: bool = False,
) -> BenchmarkResult:
    invocations = [
        payload.get("invocation", {}) for payload in _event_payloads(events, "agent_invocation")
    ]
    verifications = _event_payloads(events, "verification")
    rejections = _event_payloads(events, "revision")
    failures = _event_payloads(events, "failure")
    escalations = [
        payload
        for payload in _event_payloads(events, "escalation")
        if payload.get("available") is True
    ]
    finished = _event_payloads(events, "run_finished")
    ledger = finished[-1].get("ledger", {}) if finished else {}

    def role_count(role: str) -> int:
        return sum(1 for item in invocations if item.get("role") == role)

    models_seen: dict[str, set[str]] = {}
    for item in invocations:
        role = item.get("role")
        model = item.get("model")
        if isinstance(role, str) and isinstance(model, str) and model:
            models_seen.setdefault(role, set()).add(model)

    usage_available = bool(record.usage_available)
    cost_available = usage_available and bool(record.monetary_enforced)
    verification = record.verification

    return BenchmarkResult(
        run_id=record.run_id,
        task_id=task_id,
        policy=policy,
        provider=record.provider,
        provider_kind=record.provider_kind,
        simulated=simulated,
        model_policy=record.model_policy,
        models_by_role=record.models_by_role,
        models_used={role: sorted(models) for role, models in sorted(models_seen.items())},
        escalation_triggers=[
            str(payload.get("trigger", "unclassified")) for payload in escalations
        ],
        escalation_available=escalation_available,
        status=record.status,
        review_verdict=record.review.verdict if record.review is not None else None,
        verification_passed=bool(verification and verification.passed),
        verification_attempts=len(verifications),
        verification_diagnostic=bool(verification and verification.diagnostic),
        verification_gating=bool(verification and not verification.diagnostic),
        verification_exit_code=verification.exit_code if verification else None,
        agent_calls=record.agent_calls,
        planner_calls=role_count("planner"),
        coder_calls=role_count("coder"),
        reviewer_calls=role_count("reviewer"),
        retries=sum(1 for item in invocations if int(item.get("attempt", 1) or 1) > 1),
        escalations=record.escalations,
        revisions=record.revisions,
        reviewer_rejections=len(rejections),
        invalid_reviews=sum(1 for item in failures if item.get("reason") == "malformed_review"),
        evidence_gap_events=sum(1 for item in failures if item.get("reason") == "evidence_gap"),
        limit_exhausted=record.status in LIMIT_STATUSES,
        denied_calls=int(ledger.get("denied", 0) or 0),
        duration_ms=record.duration_ms,
        input_tokens=(
            sum(int(item.get("input_tokens", 0) or 0) for item in invocations)
            if usage_available
            else None
        ),
        output_tokens=(
            sum(int(item.get("output_tokens", 0) or 0) for item in invocations)
            if usage_available
            else None
        ),
        usage_available=usage_available,
        cost_usd=round(record.cost_usd, 8) if cost_available else None,
        monetary_enforced=bool(record.monetary_enforced),
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


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def _mean(total: float, count: int) -> float | None:
    return round(total / count, 2) if count else None


# Every status the harness may observe is counted explicitly, so a run can never
# disappear from the aggregates just because its outcome is unusual.
KNOWN_STATUSES = (
    "completed",
    "completed_with_findings",
    "review_invalid",
    "failed",
    "budget_exceeded",
    "limit_exceeded",
)

METRIC_DEFINITIONS = {
    "success_rate": "runs with status 'completed' (reviewer approved and gating "
    "verification passed) / all runs",
    "verification_pass_rate": "gating verifications that passed / runs whose "
    "recorded verification gated the outcome; diagnostic verifications after a "
    "rejection are counted separately and never raise this rate",
    "mean_agent_calls": "sum of record.agent_calls / all runs (ledger calls, so a "
    "refused call is not counted)",
    "mean_revisions": "sum of record.revisions / all runs (dispatched revision "
    "rounds, including rounds dispatched by escalation)",
    "total_rejections": "review events that requested changes and were recorded "
    "as a rejection; malformed rejections are counted in total_invalid_reviews",
    "total_invalid_reviews": "rejections that named no finding (status "
    "review_invalid)",
    "total_evidence_gaps": "blocker findings recorded with less than the minimum "
    "evidence; advisory, they do not change the verdict",
    "retries": "invocations whose attempt index is above 1, which includes "
    "coding retries, revision rounds, and verification repair rounds",
    "cost_usd": "measured only when token usage was reported and pricing is "
    "configured; otherwise null",
    "input_tokens": "reported token usage only; null when the provider did not "
    "report it",
}


def _outcomes(items: list[BenchmarkResult]) -> dict[str, int]:
    counts = {status: 0 for status in KNOWN_STATUSES}
    for item in items:
        if item.status in counts:
            counts[item.status] += 1
        else:
            counts["other"] = counts.get("other", 0) + 1
    counts.setdefault("other", 0)
    return counts


def _aggregate(items: list[BenchmarkResult]) -> dict[str, object]:
    runs = len(items)
    outcomes = _outcomes(items)
    gating = [
        item
        for item in items
        if item.verification_attempts > 0 and not item.verification_diagnostic
    ]
    diagnostic = [item for item in items if item.verification_diagnostic]
    usage_available = bool(items) and all(item.usage_available for item in items)
    cost_available = usage_available and all(item.monetary_enforced for item in items)
    gating_passed = sum(1 for item in gating if item.verification_passed)
    triggers: dict[str, int] = {}
    for item in items:
        for trigger in item.escalation_triggers:
            triggers[trigger] = triggers.get(trigger, 0) + 1
    return {
        "runs": runs,
        "outcomes": outcomes,
        "completed": outcomes["completed"],
        "completed_with_findings": outcomes["completed_with_findings"],
        "review_invalid": outcomes["review_invalid"],
        "failed": outcomes["failed"],
        "budget_exceeded": outcomes["budget_exceeded"],
        "limit_exceeded": outcomes["limit_exceeded"],
        "other": outcomes["other"],
        "limit_exhausted": sum(1 for item in items if item.limit_exhausted),
        "success_rate": _ratio(outcomes["completed"], runs),
        "verified": sum(1 for item in items if item.verification_passed),
        "verification_runs": sum(
            1 for item in items if item.verification_attempts > 0
        ),
        "gating_verification_runs": len(gating),
        "gating_verified": gating_passed,
        "verification_pass_rate": _ratio(gating_passed, len(gating)),
        "diagnostic_verification_runs": len(diagnostic),
        "mean_agent_calls": _mean(sum(i.agent_calls for i in items), runs),
        "mean_planner_calls": _mean(sum(i.planner_calls for i in items), runs),
        "mean_coder_calls": _mean(sum(i.coder_calls for i in items), runs),
        "mean_reviewer_calls": _mean(sum(i.reviewer_calls for i in items), runs),
        "mean_revisions": _mean(sum(i.revisions for i in items), runs),
        "total_retries": sum(i.retries for i in items),
        "total_escalations": sum(i.escalations for i in items),
        "escalation_triggers": dict(sorted(triggers.items())),
        "total_rejections": sum(i.reviewer_rejections for i in items),
        "total_invalid_reviews": sum(i.invalid_reviews for i in items),
        "total_evidence_gaps": sum(i.evidence_gap_events for i in items),
        "mean_duration_ms": (
            round(sum(i.duration_ms for i in items) / runs, 1) if runs else None
        ),
        "input_tokens": (
            sum(int(i.input_tokens or 0) for i in items) if usage_available else None
        ),
        "output_tokens": (
            sum(int(i.output_tokens or 0) for i in items) if usage_available else None
        ),
        "cost_usd": (
            round(sum(float(i.cost_usd or 0.0) for i in items), 8)
            if cost_available
            else None
        ),
        "usage_available": usage_available,
    }


def _task_matrix(results: list[BenchmarkResult]) -> list[dict[str, object]]:
    """One row per (task, policy), including policies outside POLICY_ORDER."""
    policies = [name for name in POLICY_ORDER if any(i.policy == name for i in results)]
    policies += sorted({item.policy for item in results} - set(policies))

    rows: list[dict[str, object]] = []
    for task_id in sorted({item.task_id for item in results}):
        for policy in policies:
            for item in results:
                if item.task_id != task_id or item.policy != policy:
                    continue
                rows.append(
                    {
                        "task_id": item.task_id,
                        "policy": item.policy,
                        "run_id": item.run_id,
                        "models_used": item.models_used,
                        "escalation_triggers": item.escalation_triggers,
                        "status": item.status,
                        "verified": item.verification_passed,
                        "verification": _verification_label(item),
                        "calls": item.agent_calls,
                        "revisions": item.revisions,
                        "rejections": item.reviewer_rejections,
                        "invalid_reviews": item.invalid_reviews,
                        "evidence_gaps": item.evidence_gap_events,
                        "duration_ms": item.duration_ms,
                    }
                )
    return rows


def _verification_label(item: BenchmarkResult) -> str:
    if item.verification_attempts == 0:
        return "not run"
    label = "passed" if item.verification_passed else "failed"
    return f"{label} (diagnostic)" if item.verification_diagnostic else label


def _tradeoffs(aggregate: dict[str, dict[str, object]]) -> list[str]:
    """Derive trade-off statements from the measurements, never a verdict."""
    baseline = aggregate.get("fixed-fast")
    lines: list[str] = []
    for name in POLICY_ORDER:
        if name == "fixed-fast" or name not in aggregate or baseline is None:
            continue
        item = aggregate[name]
        delta_completed = int(item["completed"]) - int(baseline["completed"])
        delta_calls = float(item["mean_agent_calls"] or 0) - float(
            baseline["mean_agent_calls"] or 0
        )
        if delta_completed > 0:
            lines.append(
                f"{name} completed {delta_completed} more task(s) than fixed-fast "
                f"on this matrix, at {delta_calls:+.2f} mean agent calls per task."
            )
        elif delta_completed < 0:
            lines.append(
                f"{name} completed {-delta_completed} fewer task(s) than fixed-fast "
                f"on this matrix."
            )
        elif abs(delta_calls) < 1e-9:
            lines.append(
                f"{name} measured the same outcomes and the same mean call count "
                "as fixed-fast on this matrix."
            )
        else:
            lines.append(
                f"{name} matched fixed-fast's outcome count while using "
                f"{delta_calls:+.2f} mean agent calls per task."
            )
    if not lines:
        lines.append(
            "Only one policy was measured, so no policy-to-policy trade-off is "
            "reported."
        )
    return lines


def summarise(results: list[BenchmarkResult]) -> dict[str, object]:
    """Aggregate without inventing a winner.

    A cost comparison is only emitted when every result has usage metadata and
    monetary enforcement active. Otherwise the caller gets a reason instead of
    a number that would be misleading.

    Policies that are supposed to escalate but cannot (no distinct strong model
    configured) are called out explicitly. Without that warning, equal numbers
    across ``fixed-fast``, ``adaptive``, and ``multi-agent`` would read as
    evidence that the policies are equivalent, when the escalation knob was
    simply inert.
    """
    by_policy: dict[str, list[BenchmarkResult]] = {}
    for item in results:
        by_policy.setdefault(item.policy, []).append(item)

    aggregate = {name: _aggregate(items) for name, items in by_policy.items()}

    comparable = bool(results) and all(
        i.usage_available and i.monetary_enforced for i in results
    )

    escalatable = [
        name
        for name, items in by_policy.items()
        if POLICIES.get(name) and POLICIES[name].max_escalations > 0
    ]
    inert = sorted(
        name
        for name in escalatable
        if all(item.escalation_available is False for item in by_policy[name])
    )
    unknown = sorted(
        name
        for name in escalatable
        if all(item.escalation_available is None for item in by_policy[name])
    )

    warnings: list[str] = []
    if inert:
        warnings.append(
            "Escalation is inert for "
            + ", ".join(inert)
            + ": no distinct strong model is configured, so these policies "
            "cannot select a stronger model. Their results are identical to "
            "the non-escalating policy by construction, not evidence that the "
            "policies perform equivalently. Set OPENAI_COMPAT_MODEL_STRONG to a "
            "different model id to make the comparison meaningful."
        )
    if unknown:
        warnings.append(
            "Escalation availability was not recorded for "
            + ", ".join(unknown)
            + ", so it cannot be confirmed that a stronger model was reachable."
        )
    if results and any(item.simulated for item in results):
        warnings.append(
            "Scripted results are present: the orchestration, review, "
            "verification, and accounting are real, but the model behaviour for "
            "those runs is simulated, so they say nothing about the quality of "
            "any real model."
        )

    return {
        "policies": aggregate,
        "by_task": _task_matrix(results),
        "metric_definitions": METRIC_DEFINITIONS,
        "cost_comparable": comparable,
        "usage_comparable": bool(results) and all(i.usage_available for i in results),
        "cost_note": (
            "cost comparison available"
            if comparable
            else "cost comparison withheld: usage metadata or token pricing is "
            "missing, so USD figures are estimates, not comparable measurements"
        ),
        "warnings": warnings,
        "tradeoffs": _tradeoffs(aggregate),
        "note": "All policies run the same orchestration graph; they differ only in "
        "model selection and escalation. No policy is declared best from this run.",
    }


def config_fingerprint(settings) -> dict[str, object]:
    """The configuration a result must be read against.

    Includes every setting that changes what a run does or can afford: models and
    routing, pricing and enforcement, the limit ceilings that clamp scenario
    limits, and the verification environment.
    """
    return {
        "provider_mode": settings.provider,
        "fast_model": settings.fast_model or None,
        "strong_model": settings.strong_model or None,
        "role_models": settings.role_models or None,
        "escalation_available": settings.escalation_available,
        "pricing_known": settings.pricing_known,
        "monetary_enforced": settings.monetary_enforced,
        "json_mode": settings.json_mode,
        "retry_backoff_seconds": settings.retry_backoff_seconds,
        "snapshot_char_limit": settings.snapshot_char_limit,
        "execution_enabled": settings.execution_enabled,
        "verification_command": settings.verification_command,
        "verification_timeout_seconds": settings.verification_timeout_seconds,
        "allowed_commands": sorted(settings.allowed_command_set),
        "max_agent_calls": settings.max_agent_calls,
        "max_budget_usd": settings.max_budget_usd,
        "max_wall_seconds": settings.max_wall_seconds,
        "max_retries": settings.max_retries,
        "max_revisions": settings.max_revisions,
        "max_escalations": settings.max_escalations,
    }


def build_report(
    results: list[BenchmarkResult],
    *,
    suite: str,
    config: dict[str, object],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "suite": suite,
        "config": config,
        "config_fingerprint": fingerprint_digest(config),
        "summary": summarise(results),
        "results": [item.model_dump(mode="json") for item in results],
    }


def fingerprint_digest(config: dict[str, object]) -> str:
    """A short stable digest of the configuration, for grouping repeats."""
    return json.dumps(config, sort_keys=True, separators=(",", ":"))


def render_markdown(
    results: list[BenchmarkResult],
    summary: dict[str, object],
    *,
    config: dict[str, object],
) -> str:
    policies: dict[str, dict[str, object]] = summary["policies"]  # type: ignore[assignment]
    rows: list[dict[str, object]] = summary["by_task"]  # type: ignore[assignment]
    lines: list[str] = ["# SwarmOS policy benchmark", ""]

    lines.append("## Configuration")
    lines.append("")
    for key, value in config.items():
        lines.append(f"- `{key}`: {value}")
    lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "All policies run the same orchestration graph; they differ only in model "
        "selection and escalation. These results measure routing, review, "
        "verification, and limit behaviour."
    )
    if results and any(item.simulated for item in results):
        lines.append("")
        lines.append(
            "Scripted model responses are present. The orchestration, review, and "
            "verification are real; the model behaviour is simulated, so this is "
            "not evidence about model quality."
        )
    for warning in summary["warnings"]:  # type: ignore[union-attr]
        lines.append("")
        lines.append(f"> {warning}")
    lines.append("")

    lines.append("## Results by task and policy")
    lines.append("")
    lines.append(
        "| task | policy | status | verification | calls | revisions | rejections "
        "| invalid reviews | evidence gaps | duration |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in rows:
        lines.append(
            f"| {row['task_id']} | {row['policy']} | {row['status']} "
            f"| {row['verification']} | {row['calls']} "
            f"| {row['revisions']} | {row['rejections']} "
            f"| {row['invalid_reviews']} | {row['evidence_gaps']} "
            f"| {row['duration_ms']} ms |"
        )
    lines.append("")

    lines.append("## Aggregates by policy")
    lines.append("")
    lines.append(
        "| policy | runs | approved | rejected | invalid review | failed "
        "| limit hit | success rate | verification pass rate | mean calls "
        "| mean revisions | rejections | evidence gaps |"
    )
    lines.append(
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    for name in POLICY_ORDER:
        if name not in policies:
            continue
        item = policies[name]
        lines.append(
            f"| {name} | {item['runs']} | {item['completed']} "
            f"| {item['completed_with_findings']} | {item['review_invalid']} "
            f"| {item['failed']} | {item['limit_exhausted']} "
            f"| {_rate(item['success_rate'])} | {_rate(item['verification_pass_rate'])} "
            f"| {item['mean_agent_calls']} | {item['mean_revisions']} "
            f"| {item['total_rejections']} | {item['total_evidence_gaps']} |"
        )
    lines.append("")
    lines.append(
        "Denominators: success rate is approved runs / all runs; verification pass "
        "rate is passed gating verifications / runs with a gating verification "
        f"({sum(int(p['gating_verification_runs']) for p in policies.values())} "
        "here). Diagnostic verifications after a rejection are counted separately "
        "and never raise that rate."
    )
    lines.append("")

    lines.append("## Cost and tokens")
    lines.append("")
    if summary["cost_comparable"]:
        for name in POLICY_ORDER:
            if name in policies:
                item = policies[name]
                lines.append(
                    f"- {name}: {item['input_tokens']} input / "
                    f"{item['output_tokens']} output tokens, "
                    f"${float(item['cost_usd'] or 0.0):.8f} measured"
                )
    else:
        lines.append(f"- Unavailable. {summary['cost_note']}.")
    lines.append("")

    failures = [
        item
        for item in results
        if item.status not in SUCCESS_STATUSES or item.limit_exhausted
    ]
    lines.append("## Failures and limit exhaustion")
    lines.append("")
    if failures:
        seen: set[tuple[str, str]] = set()
        for item in failures:
            key = (item.task_id, item.policy)
            if key in seen:
                continue
            seen.add(key)
            lines.append(
                f"- {item.task_id} / {item.policy}: {item.status}"
                + (f" ({item.error})" if item.error else "")
                + (
                    f"; {item.denied_calls} call(s) refused by a limit"
                    if item.denied_calls
                    else ""
                )
            )
    else:
        lines.append("- None. Every run ended in an approved or findings outcome.")
    lines.append("")

    lines.append("## Run identifiers")
    lines.append("")
    lines.append("Each row above is one persisted run; these are its run ids.")
    lines.append("")
    for row in rows:
        models = row.get("models_used") or {}
        detail = "; ".join(
            f"{role}={','.join(models[role])}" for role in sorted(models)
        )
        suffix = f"; models: {detail}" if detail else ""
        lines.append(f"- {row['task_id']} / {row['policy']}: `{row['run_id']}`{suffix}")
    lines.append("")

    lines.append("## Escalations")
    lines.append("")
    escalated = [item for item in results if item.escalation_triggers]
    if escalated:
        for item in escalated:
            lines.append(
                f"- {item.task_id} / {item.policy}: "
                + ", ".join(item.escalation_triggers)
            )
    else:
        lines.append("- None. No run spent an escalation.")
    lines.append("")

    lines.append("## Metric definitions")
    lines.append("")
    for name, definition in summary["metric_definitions"].items():  # type: ignore[union-attr]
        lines.append(f"- `{name}`: {definition}")
    lines.append("")

    lines.append("## Trade-offs")
    lines.append("")
    for line in summary["tradeoffs"]:  # type: ignore[union-attr]
        lines.append(f"- {line}")
    lines.append("")
    lines.append(str(summary["note"]))
    lines.append("")
    return "\n".join(lines)


def _rate(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.0%}"


def write_json_report(path: Path, report: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def write_markdown(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_results(path: Path, results: list[BenchmarkResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for item in results:
            handle.write(item.model_dump_json())
            handle.write("\n")
