from __future__ import annotations

import pytest

from app.models import Limits
from app.providers.base import ProviderError
from app.runtime.limits import (
    BudgetExceeded,
    BudgetLedger,
    CallLimitExceeded,
    DeadlineExceeded,
    estimate_cost_usd,
)
from tests.conftest import (
    PLAN_THREE_STEPS,
    ScriptedProvider,
    coder_response,
    plan_response,
    review_response,
)


def ledger(**overrides) -> BudgetLedger:
    values = {
        "max_usd": 1.0,
        "max_calls": 10,
        "max_wall_seconds": 60,
        "reservation_usd": 0.1,
    }
    values.update(overrides)
    return BudgetLedger(**values)


async def test_reserve_and_settle_tracks_exact_spend():
    book = ledger()
    reserved = await book.reserve(0.1)
    assert book.reserved_usd == pytest.approx(0.1)
    await book.settle(reserved, 0.07)
    assert book.spent_usd == pytest.approx(0.07)
    assert book.reserved_usd == 0
    assert book.calls == 1


async def test_budget_refuses_call_that_would_overshoot():
    book = ledger(max_usd=0.15, reservation_usd=0.1)
    await book.reserve(0.1)
    with pytest.raises(BudgetExceeded):
        await book.reserve(0.1)


async def test_release_returns_unused_reservation():
    book = ledger()
    reserved = await book.reserve(0.1)
    await book.release(reserved)
    assert book.reserved_usd == 0
    assert book.spent_usd == 0


async def test_call_limit_is_enforced():
    book = ledger(max_calls=1)
    reserved = await book.reserve(0.1)
    await book.settle(reserved, 0.0)
    with pytest.raises(CallLimitExceeded):
        await book.reserve(0.1)


async def test_deadline_is_enforced():
    book = ledger(max_wall_seconds=0)
    with pytest.raises(DeadlineExceeded):
        await book.reserve(0.0)


def test_cost_uses_per_million_rates():
    assert estimate_cost_usd(
        input_tokens=1_000_000,
        output_tokens=500_000,
        input_rate_per_million=2.0,
        output_rate_per_million=4.0,
    ) == pytest.approx(4.0)


async def test_run_halts_when_budget_is_exhausted(build_run):
    def handler(request, index):
        if request.role == "planner":
            return plan_response(PLAN_THREE_STEPS)
        if request.role == "coder":
            return coder_response("alpha")
        return review_response()

    provider = ScriptedProvider(handler, input_tokens=1_000_000, output_tokens=0)
    orchestrator, runtime, record = build_run(
        provider,
        limits=Limits(max_budget_usd=0.05, max_agent_calls=20),
        base_url="http://127.0.0.1:1/v1",
        fast_model="fast-model",
        fast_input_rate=1.0,
        fast_output_rate=1.0,
        reservation_usd=0.02,
    )
    result = await orchestrator.run(record)

    assert runtime.settings.monetary_enforced is True
    assert result.record.status == "budget_exceeded"
    assert result.record.monetary_enforced is True
    assert runtime.ledger.denied >= 1
    assert result.record.cost_usd > 0
    assert any(event.kind == "budget" for event in result.events)


async def test_run_halts_at_call_limit(build_run):
    def handler(request, index):
        if request.role == "planner":
            return plan_response(PLAN_THREE_STEPS)
        return coder_response(request.role)

    provider = ScriptedProvider(handler)
    orchestrator, runtime, record = build_run(
        provider,
        limits=Limits(max_agent_calls=2, max_retries=0, max_escalations=0),
    )
    result = await orchestrator.run(record)

    assert result.record.status == "limit_exceeded"
    assert runtime.ledger.calls == 2
    assert any(event.kind == "budget" for event in result.events)


async def test_a_retry_is_a_ledger_call_and_is_refused_at_the_call_limit(build_run):
    """A retry must not be able to escape the call budget."""
    attempts = {"count": 0}

    def handler(request, index):
        if request.role == "planner":
            attempts["count"] += 1
            raise ProviderError("overloaded", category="server", retryable=True)
        return review_response()

    provider = ScriptedProvider(handler)
    orchestrator, runtime, record = build_run(
        provider,
        limits=Limits(max_agent_calls=1, max_retries=1, max_escalations=0),
        retry_backoff_seconds=0,
    )
    result = await orchestrator.run(record)

    # The first attempt spent the only call; the retry was refused, not hidden.
    assert attempts["count"] == 1
    assert result.record.status == "limit_exceeded"
    assert runtime.ledger.calls == 1
    assert runtime.ledger.denied == 1
