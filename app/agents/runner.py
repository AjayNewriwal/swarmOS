"""Executes one typed agent call with budget accounting and traceability."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pydantic import BaseModel

from app.agents.base import Agent, AgentOutputError
from app.models import AgentInvocation, ModelTier, utc_now
from app.runtime.limits import LimitExceeded
from app.runtime.runtime import RunRuntime


@dataclass
class AgentCallResult:
    output: BaseModel | None
    invocations: list[AgentInvocation] = field(default_factory=list)
    error: str | None = None

    @property
    def invocation(self) -> AgentInvocation | None:
        return self.invocations[-1] if self.invocations else None

    @property
    def cost_usd(self) -> float:
        return sum(item.cost_usd for item in self.invocations)


class AgentRunner:
    def __init__(self, runtime: RunRuntime) -> None:
        self.runtime = runtime

    async def run(
        self,
        agent: Agent,
        payload: BaseModel,
        *,
        step_id: str | None = None,
        tier: ModelTier = "fast",
        attempt: int = 1,
        max_attempts: int = 1,
    ) -> AgentCallResult:
        """Call an agent, retrying provider/parse failures up to ``max_attempts``.

        Raises ``LimitExceeded`` when the ledger refuses the call; the node that
        called the runner decides how to end the run.
        """

        result = AgentCallResult(output=None)
        for offset in range(max(1, max_attempts)):
            model = self.runtime.model_for(tier)
            estimate = self.runtime.estimate_call_cost(model)
            try:
                reserved = await self.runtime.ledger.reserve(estimate)
            except LimitExceeded as exc:
                self.runtime.emit(
                    "budget",
                    f"Call refused: {exc}",
                    role=agent.role,
                    step_id=step_id,
                    reason=str(exc),
                    ledger=self.runtime.ledger.snapshot(),
                )
                raise
            invocation = AgentInvocation(
                run_id=self.runtime.run_id,
                role=agent.role,
                step_id=step_id,
                provider=self.runtime.provider_name,
                model=model,
                model_tier=tier,
                attempt=attempt + offset,
            )
            started = time.perf_counter()
            try:
                response = await self.runtime.provider.complete(
                    agent.build_request(payload, model)
                )
            except Exception as exc:  # noqa: BLE001 - provider boundary
                await self.runtime.ledger.release(reserved)
                failed = invocation.model_copy(
                    update={
                        "status": "failed",
                        "finished_at": utc_now(),
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                    }
                )
                result.invocations.append(failed)
                result.error = failed.error
                self._emit_invocation(failed)
                self.runtime.emit(
                    "failure",
                    f"{agent.role} call failed",
                    step_id=step_id,
                    attempt=failed.attempt,
                    error=failed.error,
                )
                continue

            cost = self.runtime.cost_for(
                model, response.input_tokens, response.output_tokens
            )
            await self.runtime.ledger.settle(reserved, cost)
            invocation = invocation.model_copy(
                update={
                    "status": "succeeded",
                    "finished_at": utc_now(),
                    "latency_ms": response.latency_ms,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "cost_usd": cost,
                }
            )
            result.invocations.append(invocation)

            try:
                output = agent.parse(response.text)
            except AgentOutputError as exc:
                rejected = invocation.model_copy(
                    update={"status": "failed", "error": str(exc)[:1000]}
                )
                result.invocations[-1] = rejected
                result.error = rejected.error
                self._emit_invocation(rejected)
                self.runtime.emit(
                    "failure",
                    f"{agent.role} returned unparseable output",
                    step_id=step_id,
                    attempt=rejected.attempt,
                    error=rejected.error,
                )
                continue

            result.output = output
            result.error = None
            self._emit_invocation(invocation)
            return result

        return result

    def _emit_invocation(self, invocation: AgentInvocation) -> None:
        self.runtime.emit(
            "agent_invocation",
            f"{invocation.role} {invocation.status}"
            + (f" for {invocation.step_id}" if invocation.step_id else ""),
            invocation=invocation,
            role=invocation.role,
            step_id=invocation.step_id,
            status=invocation.status,
            model=invocation.model,
            tier=invocation.model_tier,
            provider=invocation.provider,
            attempt=invocation.attempt,
            latency_ms=invocation.latency_ms,
            cost_usd=invocation.cost_usd,
            error=invocation.error,
        )