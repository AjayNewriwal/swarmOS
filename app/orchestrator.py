"""The SwarmOS orchestration graph.

Flow
----
planner -> (fan out independent steps) -> advance -> ... -> review -> verify
                 ^                                          |          |
                 +------------- bounded retries ------------+----------+
                                                           escalate

Routing rules are deliberately explicit and small:

* a step is dispatched only when all of its dependencies are complete, so every
  step in one fan-out is independent of the others;
* a coding step gets at most ``max_retries + 1`` attempts;
* one model escalation is allowed by default, after retries are exhausted;
* review can request changes at most ``max_revisions`` times before the run
  finalises, and each revision round actually dispatched is counted once;
* verification failures can trigger at most ``max_retries`` repair rounds;
* the ledger refuses any call that would exceed the budget, call, or time limit.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from app.agents.coder import CodingAgent
from app.agents.planner import PlannerAgent
from app.agents.reviewer import ReviewAgent
from app.agents.runner import AgentCallResult, AgentRunner
from app.agents.verifier import VerificationAgent
from app.models import (
    CodingInput,
    FileChange,
    PlannerInput,
    PlanStep,
    ReviewFinding,
    ReviewInput,
    RunRecord,
    TraceEvent,
    VerificationResult,
    utc_now,
)
from app.providers.base import ModelUnavailableError
from app.review_evidence import validate_review
from app.runtime.limits import LimitExceeded
from app.runtime.runtime import RunRuntime
from app.runtime.workspace import Workspace
from app.state import CodeStepPayload, RunHalt, StepFailure, SwarmState, initial_state

_MAX_FILES_PER_STEP = 20
_MAX_FILE_CHARS = 200_000
_HALTING = {"budget_exceeded", "limit_exceeded"}


@dataclass
class RunResult:
    record: RunRecord
    events: list[TraceEvent]
    state: SwarmState


class Orchestrator:
    def __init__(self, runtime: RunRuntime) -> None:
        self.runtime = runtime
        self.runner = AgentRunner(runtime)
        self.planner = PlannerAgent()
        self.coder = CodingAgent()
        self.reviewer = ReviewAgent()
        self.verifier = VerificationAgent(
            runtime.settings.verification_command,
            runtime.settings.verification_timeout_seconds,
        )
        self.graph = self._build()

    # ------------------------------------------------------------------
    # entry point
    # ------------------------------------------------------------------
    async def run(self, record: RunRecord) -> RunResult:
        state = initial_state(
            run_id=record.run_id,
            goal=record.goal,
            limits=self.runtime.limits.model_dump(),
            provider=self.runtime.provider_name,
            provider_warning=self.runtime.provider_warning,
            started_at=time.perf_counter(),
            started_wall=utc_now(),
        )
        state["workspace"] = self.runtime.workspace.snapshot()

        self.runtime.emit(
            "run_started",
            "Run started",
            goal=record.goal,
            provider=self.runtime.provider_name,
            provider_warning=self.runtime.provider_warning,
            limits=self.runtime.limits.model_dump(),
            files=sorted(state["workspace"].keys()),
        )
        started = time.perf_counter()

        try:
            final: SwarmState = await self.graph.ainvoke(  # type: ignore[assignment]
                state, config={"recursion_limit": 64}
            )
        except Exception as exc:  # noqa: BLE001 - graph bugs must end the run cleanly
            self.runtime.emit("failure", "Graph execution failed", error=str(exc))
            final = dict(state)  # type: ignore[assignment]
            final["status"] = "failed"
            final["error"] = f"{type(exc).__name__}: {exc}"
            final["stage"] = "done"

        duration_ms = int((time.perf_counter() - started) * 1000)
        changes: list[FileChange] = final.get("changes", []) or []
        touched = sorted({change.path for change in changes})
        workspace = final.get("workspace", {}) or {}
        artifacts = {path: workspace[path] for path in touched if path in workspace}

        invocations = final.get("agent_invocations", []) or []
        updated = record.model_copy(
            update={
                "status": final.get("status", "failed"),
                "provider": self.runtime.provider_name,
                "provider_kind": self._provider_kind(),
                "provider_warning": self.runtime.provider_warning,
                "model_policy": self.runtime.model_policy,
                "models_by_role": self._models_by_role(),
                "plan": final.get("plan"),
                "review": final.get("review"),
                "verification": final.get("verification"),
                "summary": final.get("summary", "") or self._summary(final),
                "error": final.get("error"),
                "cost_usd": round(self.runtime.ledger.spent_usd, 8),
                "monetary_enforced": self.runtime.settings.monetary_enforced,
                "usage_available": any(item.usage_available for item in invocations),
                "agent_calls": self.runtime.ledger.calls,
                "escalations": int(final.get("escalations", 0) or 0),
                "revisions": int(final.get("revisions", 0) or 0),
                "duration_ms": duration_ms,
                "updated_at": utc_now(),
                "changed_files": touched,
                "artifacts": artifacts,
            }
        )
        return RunResult(record=updated, events=list(self.runtime.trace.events), state=final)

    def _provider_kind(self) -> str:
        return self.runtime.settings.provider_kind

    def _models_by_role(self) -> dict[str, str]:
        if self.runtime.seeded_plan is not None:
            models = {"planner": "skipped (demo recipe)"}
        else:
            models = {"planner": self.runtime.selection_for_role("planner").model}
        for role in ("coder", "reviewer"):
            models[role] = self.runtime.selection_for_role(role).model
        models["verifier"] = "deterministic-subprocess"
        return models

    # ------------------------------------------------------------------
    # graph wiring
    # ------------------------------------------------------------------
    def _build(self):
        graph = StateGraph(SwarmState)
        graph.add_node("planner", self._planner)
        graph.add_node("code_step", self._code_step)
        graph.add_node("advance", self._advance)
        graph.add_node("review", self._review)
        graph.add_node("verify", self._verify)
        graph.add_node("escalate", self._escalate)
        graph.add_node("finalize", self._finalize)

        graph.add_edge(START, "planner")
        graph.add_conditional_edges(
            "planner", self._after_planner, ["code_step", "finalize"]
        )
        graph.add_edge("code_step", "advance")
        graph.add_conditional_edges(
            "advance",
            self._after_advance,
            ["code_step", "review", "escalate", "finalize"],
        )
        graph.add_conditional_edges(
            "review",
            self._after_review,
            ["code_step", "verify", "escalate", "finalize"],
        )
        graph.add_conditional_edges(
            "verify", self._after_verify, ["code_step", "escalate", "finalize"]
        )
        graph.add_conditional_edges(
            "escalate",
            self._after_escalate,
            ["code_step", "review", "verify", "finalize"],
        )
        graph.add_edge("finalize", END)
        return graph.compile()

    # ------------------------------------------------------------------
    # nodes
    # ------------------------------------------------------------------
    async def _planner(self, state: SwarmState) -> dict:
        if self.runtime.seeded_plan is not None:
            plan = self.runtime.seeded_plan
            self.runtime.emit(
                "routing_decision",
                "Plan supplied by the demo task recipe; planner model skipped",
                decision="plan_seeded",
                planner_skipped=True,
                recipe=self.runtime.demo_task,
                steps=[step.id for step in plan.steps],
                waves=[[step.id for step in wave] for wave in plan.waves()],
                assumptions=plan.assumptions,
            )
            return {
                "plan": plan,
                "workspace": self.runtime.workspace.snapshot(),
                "stage": "coding",
            }

        limits = self.runtime.limits
        self.runtime.emit(
            "routing_decision",
            "Dispatching the planner",
            decision="plan",
            reason="Every run starts with a typed plan.",
            tier=state.get("model_tier", "fast"),
        )
        try:
            result = await self.runner.run(
                self.planner,
                PlannerInput(goal=state["goal"]),
                tier=state.get("model_tier", "fast"),
                attempt=1,
                max_attempts=1 + limits.max_retries,
            )
        except LimitExceeded as exc:
            return {"status": exc.status, "error": str(exc), "stage": "done"}
        except ModelUnavailableError as exc:
            return {"status": "failed", "error": str(exc), "stage": "done"}

        updates: dict = self._call_updates(result)
        if result.output is None:
            updates.update(
                status="failed",
                error=f"Planner failed: {result.error}",
                stage="done",
            )
            return updates

        plan = result.output
        updates.update(
            plan=plan,
            workspace=self.runtime.workspace.snapshot(),
            stage="coding",
        )
        self.runtime.emit(
            "routing_decision",
            f"Plan accepted with {len(plan.steps)} step(s)",
            decision="plan_accepted",
            steps=[step.id for step in plan.steps],
            waves=[[step.id for step in wave] for wave in plan.waves()],
            assumptions=plan.assumptions,
        )
        return updates

    async def _code_step(self, state: CodeStepPayload) -> dict:
        step = PlanStep.model_validate(state["step"])
        attempt = int(state.get("attempt", 1))
        findings = [ReviewFinding.model_validate(item) for item in state.get("review_findings", [])]
        payload = CodingInput(
            goal=state["goal"],
            step=step,
            files=state.get("files", {}),
            review_findings=findings,
            verification_stdout=state.get("verification_stdout", ""),
            allowed_files=sorted(self.runtime.allowed_files or []),
            read_only_files=sorted(self.runtime.read_only_files),
        )

        checkout_dir = self.runtime.step_root / state["workspace_subdir"]
        checkout = Workspace(checkout_dir)
        checkout.seed(payload.files)
        try:
            result = await self.runner.run(
                self.coder,
                payload,
                step_id=step.id,
                tier=state.get("model_tier", "fast"),
                attempt=attempt,
                max_attempts=1,
            )
        except LimitExceeded as exc:
            return {
                "halt": [RunHalt(status=exc.status, error=str(exc))],
                "attempts": {step.id: attempt},
            }
        finally:
            shutil.rmtree(checkout_dir, ignore_errors=True)

        updates: dict = self._call_updates(result)
        updates["attempts"] = {step.id: attempt}
        # A revision round is counted once per dispatch, not once per branch, so
        # every branch reports the same round number and the reducer keeps the max.
        if state.get("mode") == "revision" and int(state.get("revision_round", 0)) > 0:
            updates["revisions"] = int(state["revision_round"])

        if result.output is None:
            updates["failures"] = [
                StepFailure(
                    step_id=step.id,
                    stage="coding",
                    error=result.error or "coding agent failed",
                    attempts=attempt,
                )
            ]
            return updates

        if result.output.step_id != step.id:
            updates["failures"] = [
                StepFailure(
                    step_id=step.id,
                    stage="coding",
                    error=f"coder answered for {result.output.step_id!r}",
                    attempts=attempt,
                )
            ]
            return updates

        changes = [
            change.model_copy(update={"step_id": step.id})
            for change in result.output.changes
        ]
        disallowed = [
            change.path for change in changes if not self._path_allowed(change.path)
        ]
        if disallowed:
            allowed = sorted(self.runtime.allowed_files or [])
            read_only = sorted(self.runtime.read_only_files)
            self.runtime.emit(
                "failure",
                "Rejected a coder write outside the allowed files",
                step_id=step.id,
                paths=disallowed,
                allowed=allowed,
                read_only=read_only,
            )
            updates["failures"] = [
                StepFailure(
                    step_id=step.id,
                    stage="coding",
                    error=(
                        f"disallowed write(s): {', '.join(disallowed)}; "
                        f"allowed={allowed or 'all'}, read_only={read_only or 'none'}"
                    ),
                    attempts=attempt,
                )
            ]
            return updates
        if len(changes) > _MAX_FILES_PER_STEP:
            updates["failures"] = [
                StepFailure(
                    step_id=step.id,
                    stage="coding",
                    error=f"coder returned {len(changes)} file changes; limit is {_MAX_FILES_PER_STEP}",
                    attempts=attempt,
                )
            ]
            return updates
        oversized = [change.path for change in changes if len(change.content) > _MAX_FILE_CHARS]
        if oversized:
            updates["failures"] = [
                StepFailure(
                    step_id=step.id,
                    stage="coding",
                    error=f"file content too large: {', '.join(oversized)}",
                    attempts=attempt,
                )
            ]
            return updates

        checkout.apply(changes)
        # A branch reports success on ``coded_steps``; only the serial advance
        # node may mark a step complete, after merge validation.
        updates.update(
            changes=changes,
            coded_steps=[step.id],
        )
        return updates

    async def _advance(self, state: SwarmState) -> dict:
        changes: list[FileChange] = list(state.get("changes", []) or [])
        start = int(state.get("merged_changes", 0) or 0)
        delta = changes[start:]

        by_path: dict[str, str] = {}
        conflicts: set[str] = set()
        for change in delta:
            owner = change.step_id or "unknown"
            previous = by_path.get(change.path)
            if previous is not None and previous != owner:
                conflicts.update({previous, owner})
            by_path.setdefault(change.path, owner)

        applied = 0
        for change in sorted(delta, key=lambda item: (item.step_id or "", item.path)):
            if change.step_id in conflicts:
                continue
            self.runtime.workspace.apply([change])
            applied += 1

        failures = [
            StepFailure(
                step_id=step_id,
                stage="coding",
                error=(
                    "parallel write conflict: independent steps edited the same file; "
                    "the planner must add a dependency or separate the target files"
                ),
                attempts=int((state.get("attempts") or {}).get(step_id, 1)),
            )
            for step_id in sorted(conflicts)
        ]
        if failures:
            self.runtime.emit(
                "failure",
                "Rejected conflicting parallel writes",
                steps=sorted(conflicts),
                files=sorted(
                    {change.path for change in delta if change.step_id in conflicts}
                ),
            )

        coded: list[str] = list(state.get("coded_steps", []) or [])
        new_coded = coded[int(state.get("merged_coded", 0) or 0) :]
        accepted = [step_id for step_id in new_coded if step_id not in conflicts]
        completed = list(dict.fromkeys([*(state.get("completed_steps") or []), *accepted]))

        workspace = self.runtime.workspace.snapshot()
        self.runtime.emit(
            "routing_decision",
            f"Merged {applied} file change(s) into the workspace",
            decision="merge_wave",
            applied=applied,
            conflicts=sorted(conflicts),
            accepted_steps=accepted,
            rejected_steps=sorted(conflicts),
            files=sorted({change.path for change in delta if change.path in workspace}),
        )
        return {
            "workspace": workspace,
            "completed_steps": completed,
            "merged_changes": len(changes),
            "merged_coded": len(coded),
            "failures": failures,
            "stage": "coding",
        }

    async def _review(self, state: SwarmState) -> dict:
        plan = state.get("plan")
        if plan is None:
            return {"status": "failed", "error": "review reached without a plan"}
        limits = self.runtime.limits
        payload = ReviewInput(
            goal=state["goal"],
            plan=plan,
            changes=list(state.get("changes", []) or []),
            files=self.runtime.workspace.snapshot(),
        )
        try:
            result = await self.runner.run(
                self.reviewer,
                payload,
                tier=state.get("model_tier", "fast"),
                attempt=1,
                max_attempts=1 + limits.max_retries,
            )
        except LimitExceeded as exc:
            return {"status": exc.status, "error": str(exc), "stage": "done"}

        updates: dict = self._call_updates(result)
        updates["stage"] = "review"
        if result.output is None:
            updates.update(status="failed", error=f"Review failed: {result.error}")
            return updates

        review = result.output
        updates["review"] = review
        evidence = validate_review(
            review,
            acceptance_criteria=[
                criterion
                for step in plan.steps
                for criterion in step.acceptance_criteria
            ],
            files=payload.files,
        )
        if review.verdict == "request_changes" and evidence.is_invalid_rejection:
            # The rejection rests entirely on blockers with no minimum evidence.
            # Do not approve and do not dispatch a revision: there is nothing
            # actionable to send back to the coder. The run ends in an explicit
            # non-approval state instead of a normal "changes requested" outcome.
            self.runtime.emit(
                "failure",
                "Review rejected without substantiated evidence",
                findings=[
                    check.finding.model_dump(mode="json")
                    for check in evidence.unsupported_blockers
                ],
                missing=[
                    {
                        "step_id": check.finding.step_id,
                        "file": check.finding.file,
                        "missing": list(check.missing),
                    }
                    for check in evidence.unsupported_blockers
                ],
                note=(
                    "Evidence-shape check only. It does not judge whether the code "
                    "is correct, and it never turns a rejection into approval."
                ),
            )
            updates.update(
                status="review_invalid",
                error=(
                    "Review requested changes but no blocker finding supplied the "
                    "minimum evidence (a code reference or line, a named acceptance "
                    "criterion, and expected/actual behaviour where applicable). "
                    "The review is invalid, not an approval."
                ),
                stage="done",
            )
            return updates

        if review.verdict == "request_changes":
            rejections = int(state.get("review_rejections", 0) or 0) + 1
            updates["review_rejections"] = rejections
            updates["pending_review_steps"] = self._review_targets(plan, review)
            self.runtime.emit(
                "revision",
                "Review requested changes",
                findings=[finding.model_dump(mode="json") for finding in review.findings],
                review_rejection=rejections,
                steps=updates["pending_review_steps"],
            )
        else:
            updates["pending_review_steps"] = []
        return updates

    async def _verify(self, state: SwarmState) -> dict:
        verification = self.verifier.verify(self.runtime.workspace)
        attempts = int(state.get("verification_attempts", 0) or 0) + 1
        self.runtime.emit(
            "verification",
            "Verification passed" if verification.passed else "Verification failed",
            role="verifier",
            passed=verification.passed,
            command=verification.command,
            exit_code=verification.exit_code,
            tests_passed=verification.tests_passed,
            tests_failed=verification.tests_failed,
            duration_ms=verification.duration_ms,
            stdout_tail=verification.stdout_tail,
            stderr_tail=verification.stderr_tail,
            attempt=attempts,
        )
        if not verification.passed:
            self.runtime.emit(
                "failure",
                "Verification reported failing tests",
                exit_code=verification.exit_code,
                tests_failed=verification.tests_failed,
                stdout_tail=verification.stdout_tail,
                stderr_tail=verification.stderr_tail,
            )
        return {
            "verification": verification,
            "verification_attempts": attempts,
            "stage": "verification",
        }

    async def _escalate(self, state: SwarmState) -> dict:
        if not self.runtime.escalation_available:
            # Defensive: routing already refuses this, but a silent downgrade to
            # the fast model would misrepresent what happened.
            message = (
                "Escalation is impossible: no distinct strong model is configured. "
                "Set OPENAI_COMPAT_MODEL_STRONG to a different model id."
            )
            self.runtime.emit(
                "escalation",
                "Escalation unavailable",
                from_tier=state.get("model_tier", "fast"),
                to_tier=None,
                available=False,
                reason=message,
            )
            return {"status": "failed", "error": message, "stage": "done"}

        escalations = int(state.get("escalations", 0) or 0) + 1
        self.runtime.emit(
            "escalation",
            f"Escalating to the strong model (round {escalations})",
            from_tier=state.get("model_tier", "fast"),
            to_tier="strong",
            available=True,
            round=escalations,
            model=self.runtime.selection_for_tier("strong").model,
            unresolved=list(
                dict.fromkeys(f.step_id for f in self._unresolved_failures(state))
            ),
        )
        return {
            "model_tier": "strong",
            "escalations": escalations,
            "stage": "coding",
        }

    async def _finalize(self, state: SwarmState) -> dict:
        review = state.get("review")
        verification = state.get("verification")
        unresolved = self._unresolved_failures(state)
        halt = state.get("halt") or []

        if halt:
            status = halt[0].status
            error = halt[0].error
        elif state.get("status") in {"failed", *_HALTING} and state.get("error"):
            status = state["status"]
            error = state.get("error")
        elif state.get("status") == "review_invalid":
            status = "review_invalid"
            error = state.get("error")
        elif unresolved:
            status = "failed"
            error = "; ".join(
                f"{failure.step_id}: {failure.error}" for failure in unresolved
            )
        elif review is not None and review.verdict == "request_changes":
            status = "completed_with_findings"
            error = None
        elif verification is not None and verification.passed:
            status = "completed"
            error = None
        elif verification is not None:
            status = "failed"
            error = "verification failed after the allowed repair rounds"
        else:
            status = "failed"
            error = "the run ended before verification"

        # A rejected run used to end with no test evidence at all. Attach a
        # diagnostic verification instead: it records ground truth without
        # approving the changes, changing the status, or re-entering coding.
        diagnostic = False
        if status in {"completed_with_findings", "review_invalid"} and verification is None:
            verification = self._diagnostic_verification()
            diagnostic = True

        summary = self._summary(
            state,
            status=status,
            error=error,
            verification=verification,
            diagnostic=diagnostic,
        )
        self.runtime.emit(
            "run_finished",
            f"Run finished: {status}",
            status=status,
            error=error,
            summary=summary,
            verification=(
                {
                    "diagnostic": diagnostic,
                    "passed": verification.passed,
                    "exit_code": verification.exit_code,
                    "tests_passed": verification.tests_passed,
                    "tests_failed": verification.tests_failed,
                }
                if verification is not None
                else None
            ),
            cost_usd=round(self.runtime.ledger.spent_usd, 8),
            agent_calls=self.runtime.ledger.calls,
            ledger=self.runtime.ledger.snapshot(),
            escalated=bool(state.get("escalations")),
        )
        return {
            "status": status,
            "error": error,
            "summary": summary,
            "stage": "done",
            "verification": verification,
        }

    def _diagnostic_verification(self) -> VerificationResult:
        """Run the real test command for a rejected run and record the evidence.

        This deliberately does not touch the verdict, status, or repair routing.
        If execution is disabled, the command is not allowed, or the verifier
        raises, the failure is recorded as a failed verification with the reason
        rather than being reported as a pass or silently omitted.
        """
        try:
            result = self.verifier.verify(self.runtime.workspace)
        except Exception as exc:  # noqa: BLE001 - never lose the diagnostic reason
            result = VerificationResult(
                passed=False,
                command=self.runtime.settings.verification_command,
                exit_code=127,
                duration_ms=0,
                stdout_tail="",
                stderr_tail=(
                    "diagnostic verification could not run: "
                    f"{type(exc).__name__}: {exc}"
                ),
                workspace_files=self.runtime.workspace.file_list(),
            )
        self.runtime.emit(
            "verification",
            (
                "Diagnostic verification passed"
                if result.passed
                else "Diagnostic verification failed"
            )
            + " (reviewer did not approve; status unchanged)",
            role="verifier",
            diagnostic=True,
            passed=result.passed,
            command=result.command,
            exit_code=result.exit_code,
            tests_passed=result.tests_passed,
            tests_failed=result.tests_failed,
            duration_ms=result.duration_ms,
            stdout_tail=result.stdout_tail,
            stderr_tail=result.stderr_tail,
        )
        return result.model_copy(update={"diagnostic": True})

    # ------------------------------------------------------------------
    # conditional routing
    # ------------------------------------------------------------------
    def _after_planner(self, state: SwarmState):
        if self._halted(state) or state.get("status") == "failed":
            return "finalize"
        if state.get("plan") is None:
            return "finalize"
        ready = self._ready_steps(state)
        if not ready:
            return "finalize"
        return self._dispatch(state, ready, reason="steps with no unresolved dependencies")

    def _after_advance(self, state: SwarmState):
        if self._halted(state) or state.get("status") == "failed":
            return "finalize"
        plan = state.get("plan")
        if plan is None:
            return "finalize"

        unresolved = self._unresolved_failures(state)
        if unresolved:
            retryable = [
                failure
                for failure in unresolved
                if self._attempts(state, failure.step_id) <= self.runtime.limits.max_retries
            ]
            if retryable:
                steps = self._steps_for(plan, [f.step_id for f in retryable])
                self.runtime.emit(
                    "retry",
                    f"Retrying {len(steps)} failed step(s)",
                    steps=[step.id for step in steps],
                    attempts={step.id: self._attempts(state, step.id) + 1 for step in steps},
                    max_retries=self.runtime.limits.max_retries,
                )
                return self._dispatch(
                    state,
                    steps,
                    reason="bounded retry after a coding failure",
                    mode="retry",
                )
            if self._can_escalate(state):
                return "escalate"
            return "finalize"

        ready = self._ready_steps(state)
        if ready:
            return self._dispatch(state, ready, reason="next dependency wave")

        if self._remaining_steps(state):
            return "finalize"
        return "review"

    def _after_review(self, state: SwarmState):
        if self._halted(state) or state.get("status") in {"failed", "review_invalid"}:
            return "finalize"
        review = state.get("review")
        if review is None:
            return "finalize"
        if review.verdict == "approve":
            return "verify"

        pending = list(state.get("pending_review_steps", []) or [])
        if (
            int(state.get("review_rejections", 0) or 0) <= self.runtime.limits.max_revisions
            and pending
        ):
            plan = state["plan"]
            steps = self._independent_batch(plan, pending)
            findings = {
                finding.step_id: [finding]
                for finding in review.findings
                if finding.step_id
            }
            if steps:
                return self._dispatch(
                    state,
                    steps,
                    findings_by_step=findings,
                    reason="review requested changes",
                    mode="revision",
                )
        if self._can_escalate(state):
            return "escalate"
        return "finalize"

    def _after_verify(self, state: SwarmState):
        if self._halted(state) or state.get("status") == "failed":
            return "finalize"
        verification = state.get("verification")
        if verification is None:
            return "finalize"
        if verification.passed:
            return "finalize"

        attempts = int(state.get("verification_attempts", 0) or 0)
        if attempts <= self.runtime.limits.max_retries:
            steps = self._repair_targets(state)
            if steps:
                return self._dispatch(
                    state,
                    steps,
                    verification_stdout="\n".join(
                        part
                        for part in (verification.stdout_tail, verification.stderr_tail)
                        if part
                    ),
                    reason="verification failed; repairing the touched files",
                    mode="repair",
                )
        if self._can_escalate(state):
            return "escalate"
        return "finalize"

    def _after_escalate(self, state: SwarmState):
        if self._halted(state):
            return "finalize"
        plan = state.get("plan")
        if plan is None:
            return "finalize"
        review = state.get("review")
        pending = list(state.get("pending_review_steps", []) or [])
        if review is not None and review.verdict == "request_changes" and pending:
            steps = self._independent_batch(plan, pending)
            findings = {
                finding.step_id: [finding]
                for finding in review.findings
                if finding.step_id
            }
            if steps:
                return self._dispatch(
                    state,
                    steps,
                    findings_by_step=findings,
                    reason="escalated revision",
                    mode="revision",
                )

        unresolved = self._unresolved_failures(state)
        if unresolved:
            steps = self._steps_for(plan, [f.step_id for f in unresolved])
            if steps:
                return self._dispatch(state, steps, reason="escalated retry", mode="retry")

        verification = state.get("verification")
        if verification is not None and not verification.passed:
            steps = self._repair_targets(state)
            if steps:
                return self._dispatch(
                    state,
                    steps,
                    verification_stdout="\n".join(
                        part
                        for part in (verification.stdout_tail, verification.stderr_tail)
                        if part
                    ),
                    reason="escalated repair",
                    mode="repair",
                )
        return "finalize"

    # ------------------------------------------------------------------
    # scheduling helpers
    # ------------------------------------------------------------------
    def _ready_steps(self, state: SwarmState) -> list[PlanStep]:
        plan = state.get("plan")
        if plan is None:
            return []
        completed = set(state.get("completed_steps", []) or [])
        return [
            step
            for step in plan.steps
            if step.id not in completed and all(dep in completed for dep in step.depends_on)
        ]

    def _remaining_steps(self, state: SwarmState) -> list[PlanStep]:
        plan = state.get("plan")
        if plan is None:
            return []
        completed = set(state.get("completed_steps", []) or [])
        return [step for step in plan.steps if step.id not in completed]

    def _unresolved_failures(self, state: SwarmState) -> list[StepFailure]:
        completed = set(state.get("completed_steps", []) or [])
        latest: dict[str, StepFailure] = {}
        for failure in state.get("failures", []) or []:
            if failure.step_id in completed:
                continue
            latest[failure.step_id] = failure
        return list(latest.values())

    def _attempts(self, state: SwarmState, step_id: str) -> int:
        return int((state.get("attempts") or {}).get(step_id, 0) or 0)

    def _path_allowed(self, path: str) -> bool:
        """Enforce request-scoped write restrictions before any change is applied."""
        if self.runtime.allowed_files is None:
            return True
        if path in self.runtime.read_only_files:
            return False
        return path in self.runtime.allowed_files

    def _can_escalate(self, state: SwarmState) -> bool:
        return (
            state.get("model_tier", "fast") == "fast"
            and self.runtime.escalation_available
            and int(state.get("escalations", 0) or 0) < self.runtime.limits.max_escalations
        )

    def _halted(self, state: SwarmState) -> bool:
        return bool(state.get("halt")) or state.get("status") in _HALTING

    def _steps_for(self, plan, step_ids: list[str]) -> list[PlanStep]:
        wanted = set(step_ids)
        return [step for step in plan.steps if step.id in wanted]

    def _review_targets(self, plan, review) -> list[str]:
        known = {step.id for step in plan.steps}
        targeted = [finding.step_id for finding in review.findings if finding.step_id in known]
        if targeted:
            return list(dict.fromkeys(targeted))
        return [step.id for step in plan.steps]

    def _independent_batch(self, plan, step_ids: list[str]) -> list[PlanStep]:
        """Pick the earliest wave that intersects ``step_ids``.

        Members of one wave have no dependencies between them, so dispatching
        them together keeps the parallel-execution invariant.
        """
        wanted = set(step_ids)
        for wave in plan.waves():
            batch = [step for step in wave if step.id in wanted]
            if batch:
                return batch
        return []

    def _repair_targets(self, state: SwarmState) -> list[PlanStep]:
        plan = state.get("plan")
        if plan is None:
            return []
        touched = {
            change.step_id
            for change in state.get("changes", []) or []
            if change.step_id
        }
        if not touched:
            return []
        return self._independent_batch(plan, sorted(touched))

    def _payload(
        self,
        state: SwarmState,
        step: PlanStep,
        *,
        findings: list[ReviewFinding] | None = None,
        verification_stdout: str = "",
        mode: str = "initial",
    ) -> CodeStepPayload:
        attempt = self._attempts(state, step.id) + 1
        return CodeStepPayload(
            run_id=state["run_id"],
            goal=state["goal"],
            step=step.model_dump(mode="json"),
            model_tier=state.get("model_tier", "fast"),
            attempt=attempt,
            files=dict(state.get("workspace", {}) or {}),
            review_findings=[
                finding.model_dump(mode="json") for finding in (findings or [])
            ],
            verification_stdout=verification_stdout,
            workspace_subdir=f"{step.id}-{attempt}",
            mode=mode,
            revision_round=(
                int(state.get("review_rejections", 0) or 0) if mode == "revision" else 0
            ),
        )

    def _dispatch(
        self,
        state: SwarmState,
        steps: list[PlanStep],
        *,
        findings_by_step: dict[str, list[ReviewFinding]] | None = None,
        verification_stdout: str = "",
        reason: str,
        mode: str = "initial",
    ) -> list[Send]:
        payloads = [
            Send(
                "code_step",
                self._payload(
                    state,
                    step,
                    findings=(findings_by_step or {}).get(step.id),
                    verification_stdout=verification_stdout,
                    mode=mode,
                ),
            )
            for step in steps
        ]
        self.runtime.emit(
            "routing_decision",
            f"Dispatching {len(steps)} coding step(s)",
            decision="dispatch_coding",
            reason=reason,
            mode=mode,
            revision_round=(
                int(state.get("review_rejections", 0) or 0) if mode == "revision" else 0
            ),
            steps=[step.id for step in steps],
            parallel=len(steps) > 1,
            tier=state.get("model_tier", "fast"),
            attempts={step.id: self._attempts(state, step.id) + 1 for step in steps},
        )
        return payloads

    # ------------------------------------------------------------------
    # misc
    # ------------------------------------------------------------------
    @staticmethod
    def _call_updates(result: AgentCallResult) -> dict:
        return {
            "agent_invocations": result.invocations,
            "agent_calls": len(result.invocations),
            "cost_usd": result.cost_usd,
        }

    def _summary(
        self,
        state: SwarmState,
        *,
        status: str | None = None,
        error: str | None = None,
        verification: VerificationResult | None = None,
        diagnostic: bool = False,
    ) -> str:
        plan = state.get("plan")
        review = state.get("review")
        if verification is None:
            verification = state.get("verification")
        completed = sorted(set(state.get("completed_steps", []) or []))
        unresolved = self._unresolved_failures(state)
        lines: list[str] = []
        lines.append(f"Status: {status or state.get('status', 'unknown')}")
        if plan is None:
            lines.append("Plan: not produced")
        else:
            lines.append(
                f"Plan: {len(plan.steps)} step(s); completed {len(completed)}/{len(plan.steps)}"
            )
            lines.append("Steps: " + ", ".join(completed) if completed else "Steps: none completed")
        lines.append(
            "Review: " + (review.verdict if review is not None else "not reached")
        )
        if verification is not None:
            label = "Verification (diagnostic)" if diagnostic else "Verification"
            lines.append(
                f"{label}: "
                + ("passed" if verification.passed else "failed")
                + f" ({verification.tests_passed} passed, {verification.tests_failed} failed, "
                f"exit {verification.exit_code})"
            )
            if diagnostic:
                lines.append(
                    "Diagnostic verification does not change the review verdict."
                )
        else:
            lines.append("Verification: not run")
        if unresolved:
            lines.append(
                "Unresolved: "
                + "; ".join(f"{failure.step_id}: {failure.error}" for failure in unresolved)
            )
        if error:
            lines.append(f"Error: {error}")
        if unresolved and not self.runtime.escalation_available:
            lines.append(
                "Escalation: unavailable - no distinct strong model is configured "
                "(OPENAI_COMPAT_MODEL_STRONG)"
            )
        lines.append(
            f"Provider: {self.runtime.provider_name}; policy: {self.runtime.model_policy}"
        )
        lines.append(
            "Models: "
            + ", ".join(
                f"{role}={model}" for role, model in self._models_by_role().items()
            )
        )
        if self.runtime.settings.monetary_enforced:
            lines.append(
                f"Monetary budget: soft cap of ${self.runtime.limits.max_budget_usd:.4f} "
                "(token rates configured; this is not a hard limit, because "
                "reservations settle after each call, so a call already in flight "
                "can exceed the cap by its own cost and several parallel in-flight "
                "calls can collectively exceed it)"
            )
        else:
            lines.append(
                "Monetary budget: NOT enforced - token pricing is unknown or the "
                "provider is the demo fixture. Call, time, retry, and revision "
                "limits are the effective controls."
            )
        lines.append(
            f"Cost estimate: ${self.runtime.ledger.spent_usd:.6f}; "
            f"calls: {self.runtime.ledger.calls}; "
            f"usage metadata: {'available' if any(i.usage_available for i in (state.get('agent_invocations') or [])) else 'unavailable'}; "
            f"escalations: {int(state.get('escalations', 0) or 0)}; "
            f"revisions: {int(state.get('revisions', 0) or 0)}"
        )
        return "\n".join(lines)