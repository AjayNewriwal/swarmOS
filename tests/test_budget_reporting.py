from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.benchmark import BenchmarkResult
from app.config import Settings
from app.models import Limits, RunRecord
from app.orchestrator import Orchestrator
from app.runtime.limits import BudgetExceeded, BudgetLedger
from app.runtime.runtime import RunRuntime
from tests.conftest import (
    SEED_OK,
    ScriptedProvider,
    coder_response,
    plan_response,
    review_response,
)

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


def priced_settings(**overrides) -> Settings:
    values = {
        "provider": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "api_key": "",
        "fast_model": "fast-model",
        "strong_model": "",
        "fast_input_rate": 1.0,
        "fast_output_rate": 1.0,
        "db_path": ":memory:",
    }
    values.update(overrides)
    return Settings(**values)


def test_priced_settings_report_monetary_enforcement():
    assert priced_settings().monetary_enforced is True
    assert priced_settings(fast_output_rate=0.0).monetary_enforced is False


def test_summary_never_claims_a_hard_cap():
    """The run summary must not overstate what a USD cap guarantees."""

    def handler(request, index):
        if request.role == "planner":
            return plan_response(SINGLE_STEP_PLAN)
        if request.role == "coder":
            return coder_response("solo")
        return review_response()

    provider = ScriptedProvider(handler)
    runtime = RunRuntime(
        run_id="run_soft_cap",
        settings=priced_settings(),
        limits=Limits(max_budget_usd=0.5),
        provider=provider,
        provider_name="openai-compatible:local",
        provider_warning=None,
    )
    runtime.seed_workspace(SEED_OK, Path("missing"))
    record = RunRecord(goal="do the thing", limits=runtime.limits)

    result = asyncio.run(Orchestrator(runtime).run(record))
    summary = result.record.summary

    assert "Monetary budget" in summary
    assert "soft cap" in summary
    assert "not a hard limit" in summary
    assert "collectively exceed" in summary
    assert "enforced at" not in summary


def test_unpriced_summary_still_says_not_enforced():
    provider = ScriptedProvider(lambda request, index: plan_response(SINGLE_STEP_PLAN))
    settings = priced_settings(fast_input_rate=0.0, fast_output_rate=0.0)
    runtime = RunRuntime(
        run_id="run_no_price",
        settings=settings,
        limits=Limits(),
        provider=provider,
        provider_name="openai-compatible:local",
        provider_warning=None,
    )
    runtime.seed_workspace(SEED_OK, Path("missing"))
    record = RunRecord(goal="do the thing", limits=runtime.limits)
    result = asyncio.run(Orchestrator(runtime).run(record))
    assert "NOT enforced" in result.record.summary


def test_zero_budget_with_pricing_refuses_calls():
    """A zero cap is a hard refusal once rates exist, which is why the CLI warns."""
    ledger = BudgetLedger(
        max_usd=0.0,
        max_calls=12,
        max_wall_seconds=120,
        reservation_usd=0.02,
        monetary_enforced=True,
    )
    with pytest.raises(BudgetExceeded):
        asyncio.run(ledger.reserve(0.02))


def test_zero_budget_without_pricing_allows_calls():
    ledger = BudgetLedger(
        max_usd=0.0,
        max_calls=12,
        max_wall_seconds=120,
        reservation_usd=0.02,
        monetary_enforced=False,
    )
    assert asyncio.run(ledger.reserve(0.0)) == 0.0


def test_cli_warns_on_zero_budget_when_priced(capsys, monkeypatch):
    from app import cli

    monkeypatch.setattr(cli, "settings", priced_settings())
    cli._warn_zero_budget("openai")
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "--budget 0" in captured.err


def test_cli_does_not_warn_without_pricing(capsys, monkeypatch):
    from app import cli

    monkeypatch.setattr(
        cli, "settings", priced_settings(fast_input_rate=0.0, fast_output_rate=0.0)
    )
    cli._warn_zero_budget("openai")
    assert "WARNING" not in capsys.readouterr().err


def test_cli_does_not_warn_for_demo_provider(capsys, monkeypatch):
    from app import cli

    monkeypatch.setattr(cli, "settings", priced_settings())
    cli._warn_zero_budget("demo")
    assert "WARNING" not in capsys.readouterr().err


def test_benchmark_result_carries_escalation_availability():
    item = BenchmarkResult(
        run_id="run_x",
        task_id="t",
        policy="adaptive",
        provider="openai-compatible:local",
        provider_kind="local",
        model_policy="adaptive",
        escalation_available=False,
        status="completed",
    )
    assert item.escalation_available is False
    assert item.model_dump()["escalation_available"] is False
