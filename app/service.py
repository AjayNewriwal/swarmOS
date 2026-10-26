"""Run lifecycle: resolve a provider, prepare a record, execute, persist."""

from __future__ import annotations

import asyncio
import time

from app.config import Settings
from app.models import Limits, RunRecord, RunRequest, utc_now
from app.orchestrator import Orchestrator
from app.providers import resolve_provider
from app.providers.base import Provider
from app.runtime.runtime import VALID_POLICIES, RunRuntime
from app.store import RunStore


def _provider_kind(provider_name: str, settings: Settings) -> str:
    if provider_name == "demo":
        return "demo"
    if provider_name.endswith(":local"):
        return "local"
    if provider_name.endswith(":remote"):
        return "remote"
    return settings.provider_kind if settings.openai_configured else "unknown"


class RunService:
    def __init__(self, settings: Settings, store: RunStore) -> None:
        self.settings = settings
        self.store = store
        self._tasks: set[asyncio.Task] = set()

    def resolve_policy(self, requested: str | None) -> str:
        policy = (requested or self.settings.model_policy or "config").strip().lower()
        return policy if policy in VALID_POLICIES else "config"

    def effective_provider(self, requested: str) -> str:
        """A per-request choice wins; ``auto`` honours the process default.

        Without this, SWARMOS_PROVIDER was displayed but never applied.
        """
        choice = (requested or "auto").strip().lower()
        if choice != "auto":
            return choice
        configured = (self.settings.provider or "auto").strip().lower()
        return configured if configured in {"demo", "openai"} else "auto"

    def clamp_limits(self, requested: Limits, policy: str) -> Limits:
        ceiling = self.settings
        max_escalations = min(requested.max_escalations, ceiling.max_escalations)
        if policy == "fast_only":
            # A policy that pins every role to the fast profile cannot escalate;
            # silently escalating would contradict the policy.
            max_escalations = 0
        return Limits(
            max_agent_calls=min(requested.max_agent_calls, ceiling.max_agent_calls),
            max_budget_usd=min(requested.max_budget_usd, ceiling.max_budget_usd),
            max_wall_seconds=min(requested.max_wall_seconds, ceiling.max_wall_seconds),
            max_retries=min(requested.max_retries, ceiling.max_retries),
            max_revisions=min(requested.max_revisions, ceiling.max_revisions),
            max_escalations=max_escalations,
            parallel_coding=requested.parallel_coding,
        )

    def prepare(self, request: RunRequest) -> tuple[RunRecord, Provider, str]:
        provider, provider_name, warning = resolve_provider(
            self.effective_provider(request.provider), self.settings
        )
        policy = self.resolve_policy(request.model_policy)
        record = RunRecord(
            goal=request.goal,
            provider=provider_name,
            provider_kind=_provider_kind(provider_name, self.settings),
            provider_warning=warning,
            limits=self.clamp_limits(request.limits, policy),
            model_policy=policy,
            monetary_enforced=self.settings.monetary_enforced,
            status="queued",
        )
        self.store.save_run(record)
        return record, provider, policy

    async def run_request(self, request: RunRequest) -> RunRecord:
        record, provider, policy = self.prepare(request)
        return await self.execute(record, provider, request.files, policy)

    def submit(self, request: RunRequest) -> RunRecord:
        record, provider, policy = self.prepare(request)
        task = asyncio.create_task(self.execute(record, provider, request.files, policy))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return record

    async def execute(
        self,
        record: RunRecord,
        provider: Provider,
        files: dict[str, str],
        policy: str | None = None,
    ) -> RunRecord:
        running = record.model_copy(update={"status": "running", "updated_at": utc_now()})
        self.store.save_run(running)

        runtime = RunRuntime(
            run_id=record.run_id,
            settings=self.settings,
            limits=record.limits,
            provider=provider,
            provider_name=record.provider,
            provider_warning=record.provider_warning,
            store=self.store,
            model_policy=policy or record.model_policy,
        )
        runtime.seed_workspace(files, self.settings.sample_project)
        started = time.perf_counter()
        try:
            result = await Orchestrator(runtime).run(running)
            self.store.save_run(result.record)
            return result.record
        except asyncio.CancelledError:
            failed = running.model_copy(
                update={
                    "status": "failed",
                    "error": "run cancelled before completion",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "updated_at": utc_now(),
                }
            )
            self.store.save_run(failed)
            raise
        except Exception as exc:  # noqa: BLE001 - never leave a run stuck in "running"
            runtime.emit("failure", "Run crashed", error=f"{type(exc).__name__}: {exc}")
            failed = running.model_copy(
                update={
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "duration_ms": int((time.perf_counter() - started) * 1000),
                    "updated_at": utc_now(),
                }
            )
            self.store.save_run(failed)
            return failed
        finally:
            runtime.cleanup()

    async def drain(self) -> None:
        """Wait for in-flight background runs (used by tests and shutdown)."""
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)