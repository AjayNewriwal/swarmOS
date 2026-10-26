from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from app.config import settings as base_settings
from app.models import Limits, RunRecord
from app.orchestrator import Orchestrator
from app.providers.base import ModelRequest, ModelResponse
from app.runtime.runtime import RunRuntime

GOAL = "Fix the sample project so its tests pass"

PLAN_THREE_STEPS = {
    "assumptions": ["the sample workspace is the target"],
    "steps": [
        {
            "id": "alpha",
            "title": "Create alpha",
            "detail": "Write out/alpha.txt",
            "acceptance_criteria": ["out/alpha.txt exists"],
            "depends_on": [],
            "target_files": ["out/alpha.txt"],
        },
        {
            "id": "beta",
            "title": "Create beta",
            "detail": "Write out/beta.txt",
            "acceptance_criteria": ["out/beta.txt exists"],
            "depends_on": [],
            "target_files": ["out/beta.txt"],
        },
        {
            "id": "gamma",
            "title": "Create gamma",
            "detail": "Write out/gamma.txt",
            "acceptance_criteria": ["out/gamma.txt exists"],
            "depends_on": ["alpha", "beta"],
            "target_files": ["out/gamma.txt"],
        },
    ],
}

SEED_OK = {"tests/test_ok.py": "def test_ok():\n    assert True\n"}

TARGET_IMPLEMENTATION = '''"""Tag normalization helpers."""

from collections.abc import Iterable


def normalize_tags(tags: Iterable[str]) -> list[str]:
    cleaned = {tag.strip().lower() for tag in tags if tag and tag.strip()}
    return sorted(cleaned)
'''


def plan_response(plan: dict = PLAN_THREE_STEPS) -> str:
    return json.dumps(plan)


def coder_response(step_id: str, content: str = "ok\n", path: str | None = None) -> str:
    target = path or f"out/{step_id}.txt"
    return json.dumps(
        {
            "step_id": step_id,
            "summary": f"Created {target}",
            "rationale": "matches the acceptance criterion",
            "changes": [{"path": target, "content": content, "action": "create"}],
        }
    )


def review_response(verdict: str = "approve", findings: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "rationale": "scripted review",
            "findings": findings or [],
        }
    )


class ScriptedProvider:
    """Provider double that can track concurrency and be scripted per role."""

    name = "scripted"

    def __init__(
        self,
        handler: Callable[[ModelRequest, int], str | Exception],
        *,
        delay: float = 0.0,
        input_tokens: int = 10,
        output_tokens: int = 10,
    ) -> None:
        self.handler = handler
        self.delay = delay
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.calls: list[ModelRequest] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        index = len(self.calls) + 1
        self.calls.append(request)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            result = self.handler(request, index)
            if isinstance(result, Exception):
                raise result
            return ModelResponse(
                text=result,
                model=request.model,
                provider=self.name,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                latency_ms=1,
            )
        finally:
            self.in_flight -= 1


def make_settings(tmp_path: Path, **overrides):
    # Tests must not depend on a developer's .env. Start from a demo-only,
    # unpriced configuration and let individual tests opt into a real endpoint.
    values = {
        "db_path": tmp_path / "test.db",
        "verification_timeout_seconds": 60,
        "reservation_usd": 0.02,
        "provider": "demo",
        "base_url": "",
        "api_key": "",
        "fast_model": "",
        "strong_model": "",
        "fast_input_rate": 0.0,
        "fast_output_rate": 0.0,
        "strong_input_rate": 0.0,
        "strong_output_rate": 0.0,
        "role_models": "",
        "model_policy": "config",
    }
    values.update(overrides)
    return replace(base_settings, **values)


@pytest.fixture
def build_run(tmp_path):
    def _build(
        provider,
        *,
        goal: str = GOAL,
        limits: Limits | None = None,
        files: dict[str, str] | None = None,
        use_sample: bool = False,
        store=None,
        **settings_overrides,
    ):
        limits = limits or Limits()
        settings = make_settings(tmp_path, **settings_overrides)
        record = RunRecord(goal=goal, limits=limits)
        runtime = RunRuntime(
            run_id=record.run_id,
            settings=settings,
            limits=limits,
            provider=provider,
            provider_name=provider.name,
            provider_warning=None,
            store=store,
        )
        sample = settings.sample_project if use_sample else tmp_path / "missing-sample"
        if files is None:
            seed = {} if use_sample else SEED_OK
        else:
            seed = files
        runtime.seed_workspace(seed, sample)
        return Orchestrator(runtime), runtime, record

    return _build


def trace_kinds(result) -> list[str]:
    return [event.kind for event in result.events]