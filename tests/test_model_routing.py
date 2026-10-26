from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from app.config import ModelProfile, Settings
from app.models import Limits, RunRequest
from app.providers.base import ModelUnavailableError, ProviderError
from app.runtime.runtime import RunRuntime
from app.service import RunService
from app.store import RunStore
from tests.conftest import (
    PLAN_THREE_STEPS,
    SEED_OK,
    ScriptedProvider,
    coder_response,
    plan_response,
    review_response,
)

FAST = "qwen2.5-coder:7b"
STRONG = "qwen2.5-coder:32b"

SINGLE_STEP_PLAN = {
    "steps": [
        {
            "id": "solo",
            "title": "Do the only step",
            "detail": "write a file",
            "acceptance_criteria": ["it exists"],
            "depends_on": [],
            "target_files": ["out/solo.txt"],
        }
    ]
}


def settings(**overrides) -> Settings:
    values = {
        "provider": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "",
        "fast_model": FAST,
        "strong_model": STRONG,
        "fast_input_rate": 0.0,
        "fast_output_rate": 0.0,
        "strong_input_rate": 0.0,
        "strong_output_rate": 0.0,
        "role_models": "",
        "model_policy": "config",
        "db_path": ":memory:",
    }
    values.update(overrides)
    return Settings(**values)


def runtime(settings_obj: Settings, policy: str | None = None) -> RunRuntime:
    return RunRuntime(
        run_id="run_test",
        settings=settings_obj,
        limits=Limits(),
        provider=ScriptedProvider(lambda request, index: ""),
        provider_name="openai-compatible:local",
        provider_warning=None,
        model_policy=policy,
    )


def test_profiles_exist_only_for_configured_distinct_models():
    profiles = settings().configured_profiles()
    assert set(profiles) == {"fast", "strong"}
    assert profiles["fast"].model == FAST
    assert profiles["strong"].model == STRONG

    same = settings(strong_model=FAST).configured_profiles()
    assert set(same) == {"fast"}
    assert settings(strong_model=FAST).escalation_available is False


def test_fast_model_alone_is_enough_and_needs_no_key():
    local = settings(strong_model="")
    assert local.openai_configured is True
    assert local.provider_kind == "local"
    assert local.escalation_available is False


def test_remote_endpoint_is_labelled_remote():
    remote = settings(base_url="https://api.example.invalid/v1")
    assert remote.provider_kind == "remote"


def test_role_routing_parses_and_filters_unknown_values():
    routed = settings(role_models="planner=strong, coder=bogus, reviewer=strong")
    assert routed.role_routing()["planner"] == "strong"
    assert routed.role_routing()["coder"] == "fast"
    assert routed.role_routing()["reviewer"] == "strong"


def test_role_routing_collapses_to_fast_without_a_strong_model():
    routed = settings(strong_model="", role_models="planner=strong")
    assert routed.role_routing() == {
        "planner": "fast",
        "coder": "fast",
        "reviewer": "fast",
    }


def test_pricing_requires_both_rates_for_every_profile():
    unpriced = settings(fast_input_rate=1.0, fast_output_rate=1.0)
    assert unpriced.pricing_known is False
    assert unpriced.monetary_enforced is False

    priced = settings(
        fast_input_rate=0.1,
        fast_output_rate=0.2,
        strong_input_rate=0.3,
        strong_output_rate=0.4,
    )
    assert priced.pricing_known is True
    assert priced.monetary_enforced is True


def test_pricing_is_not_enforced_for_the_demo_provider():
    demo = settings(base_url="", fast_model="", strong_model="")
    assert demo.monetary_enforced is False


def test_fast_only_policy_pins_every_role_to_the_fast_profile():
    rt = runtime(settings(role_models="planner=strong"), policy="fast_only")
    assert rt.selection_for_role("planner").name == "fast"
    assert rt.model_policy == "fast_only"


def test_role_routed_policy_uses_strong_where_configured():
    rt = runtime(settings(role_models="planner=strong"), policy="role_routed")
    assert rt.selection_for_role("planner").model == STRONG
    assert rt.selection_for_role("coder").model == FAST


def test_adaptive_policy_starts_every_role_fast():
    rt = runtime(settings(role_models="planner=strong"), policy="adaptive")
    assert rt.selection_for_role("planner").name == "fast"


def test_missing_strong_profile_raises_instead_of_silently_downgrading():
    rt = runtime(settings(strong_model=""))
    with pytest.raises(ModelUnavailableError, match="escalation is impossible"):
        rt.selection_for_tier("strong")


def test_unknown_policy_falls_back_to_config():
    rt = runtime(settings(), policy="nonsense")
    assert rt.model_policy == "config"


def test_service_clamps_escalations_for_fast_only():
    store = RunStore(":memory:")
    try:
        svc = RunService(settings(), store)
        clamped = svc.clamp_limits(Limits(max_escalations=2), "fast_only")
        assert clamped.max_escalations == 0
    finally:
        store.close()


def test_request_rejects_unknown_policy():
    with pytest.raises(ValidationError):
        RunRequest(goal="a" * 10, model_policy="turbo")


def test_model_profile_priced_requires_both_rates():
    assert ModelProfile("fast", FAST, 1.0, 0.0).priced is False
    assert ModelProfile("fast", FAST, 1.0, 1.0).priced is True


async def test_escalation_is_reported_unavailable_instead_of_faked(build_run):
    def handler(request, index):
        if request.role == "planner":
            return plan_response(SINGLE_STEP_PLAN)
        if request.role == "coder":
            raise ProviderError("model crashed", category="server", retryable=True)
        return review_response()

    provider = ScriptedProvider(handler)
    orchestrator, rt, record = build_run(
        provider,
        files=SEED_OK,
        limits=Limits(max_retries=1, max_escalations=1, max_revisions=0),
        base_url="http://127.0.0.1:11434/v1",
        fast_model=FAST,
        strong_model="",  # no distinct strong model
    )
    result = await orchestrator.run(record)

    coder_calls = [call for call in provider.calls if call.role == "coder"]
    assert len(coder_calls) == 2  # initial + one bounded retry, no false escalation
    assert result.record.status == "failed"
    assert result.record.escalations == 0
    assert "Escalation: unavailable" in result.record.summary
    assert rt.escalation_available is False


async def test_transient_errors_are_retried_within_one_agent_call(build_run):
    attempts = {"count": 0}

    def handler(request, index):
        if request.role == "planner":
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise ProviderError("overloaded", category="server", retryable=True)
            return plan_response(PLAN_THREE_STEPS)
        if request.role == "coder":
            step_id = json.loads(request.user)["step"]["id"]
            return coder_response(step_id)
        return review_response()

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(
        provider,
        files=SEED_OK,
        limits=Limits(max_retries=1),
        retry_backoff_seconds=0,
        base_url="http://127.0.0.1:11434/v1",
        fast_model=FAST,
    )
    result = await orchestrator.run(record)

    assert attempts["count"] == 2
    assert result.record.status == "completed"
    assert any(event.kind == "retry" for event in result.events)


async def test_auth_errors_fail_fast_without_retry(build_run):
    def handler(request, index):
        raise ProviderError("bad key", category="auth", retryable=False)

    provider = ScriptedProvider(handler)
    orchestrator, _, record = build_run(
        provider,
        limits=Limits(max_retries=3),
        retry_backoff_seconds=0,
        base_url="http://127.0.0.1:11434/v1",
        fast_model=FAST,
    )
    result = await orchestrator.run(record)

    # Initial attempt plus the runner's own retry budget, but planner/reviewer
    # classify auth as non-retryable, so only one call happens.
    assert len(provider.calls) == 1
    assert result.record.status == "failed"
    provider_events = [e for e in result.events if e.kind == "provider"]
    assert provider_events and provider_events[0].payload["category"] == "auth"
def test_process_provider_default_is_applied_to_auto_requests(tmp_path):
    store = RunStore(":memory:")
    try:
        local = RunService(settings(provider="demo"), store)
        assert local.effective_provider("auto") == "demo"
        assert local.effective_provider("openai") == "openai"

        configured = RunService(settings(provider="openai"), store)
        assert configured.effective_provider("auto") == "openai"
        assert configured.effective_provider("demo") == "demo"
    finally:
        store.close()
