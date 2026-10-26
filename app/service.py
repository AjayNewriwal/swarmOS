"""Run lifecycle: resolve a provider, prepare a record, execute, persist."""

from __future__ import annotations

import asyncio
import time

from app.config import Settings
from app.models import Limits, RunRecord, RunRequest, utc_now
from app.orchestrator import Orchestrator
from app.providers import resolve_provider
from app.providers.base import Provider
from app.runtime.runtime import RunRuntime
from app.store import RunStore


class RunService:
    def __init__(self, settings: Settings, store: RunStore) -> None:
        self.settings = settings
        self.store = store
        self._tasks: set[asyncio.Task] = set()

    def clamp_limits(self, requested: Limits) -> Limits:
        ceiling = self.settings
        return Limits(
            max_agent_calls=min(requested.max_agent_calls, ceiling.max_agent_calls),
            max_budget_usd=min(requested.max_budget_usd, ceiling.max_budget_usd),
            max_wall_seconds=min(requested.max_wall_seconds, ceiling.max_wall_seconds),
            max_retries=min(requested.max_retries, ceiling.max_retries),
            max_revisions=min(requested.max_revisions, ceiling.max_revisions),
            max_escalations=min(requested.max_escalations, ceiling.max_escalations),
            parallel_coding=requested.parallel_coding,
        )

    def prepare(self, request: RunRequest) -> tuple[RunRecord, Provider]:
        provider, provider_name, warning = resolve_provider(request.provider, self.settings)
        record = RunRecord(
            goal=request.goal,
            provider=provider_name,
            provider_warning=warning,
            limits=self.clamp_limits(request.limits),
            status="queued",
        )
        self.store.save_run(record)
        return record, provider

    async def run_request(self, request: RunRequest) -> RunRecord:
        record, provider = self.prepare(request)
        return await self.execute(record, provider, request.files)

    def submit(self, request: RunRequest) -> RunRecord:
        record, provider = self.prepare(request)
        task = asyncio.create_task(self.execute(record, provider, request.files))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return record

    async def execute(
        self, record: RunRecord, provider: Provider, files: dict[str, str]
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