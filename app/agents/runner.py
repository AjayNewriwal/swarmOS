"""Executes one typed agent call with budget accounting and traceability.

Retry policy is deliberately narrow:

* only transient failures are retried (timeout, connection, rate limit, 5xx);
* authentication, bad-request, not-found, and config errors fail immediately,
  because retrying them only burns time and money;
* unparseable structured output is retried with an explicit repair note;
* every attempt is one call against the ledger, so retries cannot escape the
  call or time limits.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from pydantic import BaseModel

from app.agents.base import Agent, AgentOutputError
from app.config import ModelProfile
from app.models import AgentInvocation, ModelTier, utc_now
from app.providers.base import ProviderError
from app.runtime.limits import LimitExceeded
from app.runtime.runtime import RunRuntime

_MAX_BACKOFF_SECONDS = 8.0


@dataclass
class AgentCallResult:
    output: BaseModel | None
    invocations: list[AgentInvocation] = field(default_factory=list)
    error: str | None = None
    error_class: str | None = None
    profile: str | None = None

    @property
    def invocation(self) -> AgentInvocation | None:
        return self.invocations[-1] if self.invocations else None

    @property
    def cost_usd(self) -> float:
        return sum(item.cost_usd for item in self.invocations)


class AgentRunner:
    def __init__(self, runtime: RunRuntime) -> None:
        self.runtime = runtime

    def _select(self, agent: Agent, tier: ModelTier) -> ModelProfile:
        if tier == "strong":
            return self.runtime.selection_for_tier("strong")
        return self.runtime.selection_for_role(agent.role)

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
        """Call an agent, retrying only classified-transient failures.

        Raises ``LimitExceeded`` when the ledger refuses the call, and
        ``ModelUnavailableError`` when an escalation tier is requested but not
        configured. The calling node decides how to end the run in both cases.
        """

        profile = self._select(agent, tier)
        result = AgentCallResult(output=None, profile=profile.name)
        repair_note: str | None = None

        for offset in range(max(1, max_attempts)):
            estimate = self.runtime.estimate_call_cost(profile)
            try:
                reserved = await self.runtime.ledger.reserve(estimate)
            except LimitExceeded as exc:
                self.runtime.emit(
                    "budget",
                    f"Call refused: {exc}",
                    role=agent.role,
                    step_id=step_id,
                    reason=str(exc),
                    category=exc.status,
                    ledger=self.runtime.ledger.snapshot(),
                )
                raise
            invocation = AgentInvocation(
                run_id=self.runtime.run_id,
                role=agent.role,
                step_id=step_id,
                provider=self.runtime.provider_name,
                model=profile.model,
                profile=profile.name,
                model_tier=tier,
                attempt=attempt + offset,
            )
            started = time.perf_counter()

            try:
                response = await self.runtime.provider.complete(
                    agent.build_request(payload, profile.model, repair_note)
                )
            except ProviderError as exc:
                await self.runtime.ledger.release(reserved)
                failed = invocation.model_copy(
                    update={
                        "status": "failed",
                        "finished_at": utc_now(),
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                        "error_class": exc.category,
                    }
                )
                result.invocations.append(failed)
                result.error = failed.error
                result.error_class = exc.category
                self._emit_invocation(failed)
                self.runtime.emit(
                    "provider",
                    f"{agent.role} provider error ({exc.category})",
                    role=agent.role,
                    step_id=step_id,
                    attempt=failed.attempt,
                    category=exc.category,
                    retryable=exc.retryable,
                    error=str(exc)[:500],
                )

                has_next = offset + 1 < max(1, max_attempts)
                if exc.retryable and has_next:
                    delay = self._backoff(offset, getattr(exc, "retry_after_seconds", None))
                    self.runtime.emit(
                        "retry",
                        f"Retrying {agent.role} after {exc.category} in {delay:.1f}s",
                        role=agent.role,
                        step_id=step_id,
                        category=exc.category,
                        attempt=failed.attempt,
                        delay_seconds=round(delay, 2),
                    )
                    await asyncio.sleep(delay)
                    continue

                self.runtime.emit(
                    "failure",
                    f"{agent.role} call failed ({exc.category})",
                    step_id=step_id,
                    attempt=failed.attempt,
                    error=failed.error,
                )
                return result
            except Exception as exc:  # noqa: BLE001 - unexpected provider boundary
                await self.runtime.ledger.release(reserved)
                failed = invocation.model_copy(
                    update={
                        "status": "failed",
                        "finished_at": utc_now(),
                        "latency_ms": int((time.perf_counter() - started) * 1000),
                        "error": f"{type(exc).__name__}: {exc}"[:1000],
                        "error_class": "server",
                    }
                )
                result.invocations.append(failed)
                result.error = failed.error
                result.error_class = "server"
                self._emit_invocation(failed)
                self.runtime.emit(
                    "failure",
                    f"{agent.role} call crashed",
                    step_id=step_id,
                    attempt=failed.attempt,
                    error=failed.error,
                )
                return result

            cost = self.runtime.cost_for(
                profile, response.input_tokens, response.output_tokens
            )
            await self.runtime.ledger.settle(reserved, cost)
            invocation = invocation.model_copy(
                update={
                    "status": "succeeded",
                    "finished_at": utc_now(),
                    "latency_ms": response.latency_ms,
                    "input_tokens": response.input_tokens,
                    "output_tokens": response.output_tokens,
                    "usage_available": response.usage_available,
                    "structured_mode": response.structured_mode,
                    "cost_usd": cost,
                }
            )
            result.invocations.append(invocation)

            try:
                output = agent.parse(response.text)
            except AgentOutputError as exc:
                rejected = invocation.model_copy(
                    update={
                        "status": "failed",
                        "error": str(exc)[:1000],
                        "error_class": "invalid_response",
                    }
                )
                result.invocations[-1] = rejected
                result.error = rejected.error
                result.error_class = "invalid_response"
                self._emit_invocation(rejected)

                has_next = offset + 1 < max(1, max_attempts)
                repair_note = str(exc)[:600]
                self.runtime.emit(
                    "repair" if has_next else "failure",
                    "Requesting a structured-output repair" if has_next
                    else f"{agent.role} returned unparseable output",
                    step_id=step_id,
                    attempt=rejected.attempt,
                    error=rejected.error,
                )
                if has_next:
                    await asyncio.sleep(self._backoff(offset, None))
                    continue
                return result

            result.output = output
            result.error = None
            result.error_class = None
            self._emit_invocation(invocation, output_summary=agent.summarize(output))
            return result

        return result

    # ------------------------------------------------------------------
    def _backoff(self, offset: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(_MAX_BACKOFF_SECONDS, max(0.0, retry_after))
        base = max(0.0, self.runtime.settings.retry_backoff_seconds)
        return min(_MAX_BACKOFF_SECONDS, base * (2**offset))

    def _emit_invocation(
        self,
        invocation: AgentInvocation,
        output_summary: dict[str, object] | None = None,
    ) -> None:
        self.runtime.emit(
            "agent_invocation",
            f"{invocation.role} {invocation.status}"
            + (f" for {invocation.step_id}" if invocation.step_id else ""),
            invocation=invocation,
            role=invocation.role,
            step_id=invocation.step_id,
            status=invocation.status,
            provider=invocation.provider,
            model=invocation.model,
            profile=invocation.profile,
            tier=invocation.model_tier,
            attempt=invocation.attempt,
            latency_ms=invocation.latency_ms,
            input_tokens=invocation.input_tokens,
            output_tokens=invocation.output_tokens,
            usage_available=invocation.usage_available,
            structured_mode=invocation.structured_mode,
            cost_usd=invocation.cost_usd,
            error_class=invocation.error_class,
            error=invocation.error,
            output_summary=output_summary or {},
        )