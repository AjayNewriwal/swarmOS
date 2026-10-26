from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from tests.conftest import make_settings

GOAL = "Implement tag normalization so the failing tests pass"


@pytest.fixture
def client(tmp_path, monkeypatch):
    test_settings = make_settings(tmp_path, provider="demo")
    monkeypatch.setattr(main_module, "settings", test_settings)
    with TestClient(main_module.app) as test_client:
        yield test_client


def test_health_reports_provider_mode(client):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "swarmos"


def test_config_exposes_caps_and_sample_files(client):
    body = client.get("/api/config").json()
    assert body["caps"]["max_agent_calls"] > 0
    assert body["verification"]["command"]
    assert body["verification"]["execution_enabled"] is True
    assert "target.py" in body["sample_files"]
    assert body["provider_kind"] in {"demo", "local", "remote"}
    assert body["demo_fixture"] is True
    assert body["role_routing"] == {
        "planner": "fast",
        "coder": "fast",
        "reviewer": "fast",
    }
    assert body["monetary_enforced"] is False
    assert body["api"]["auth_required"] is False


def test_provider_models_reports_demo_without_calling_out(client):
    body = client.get("/api/provider/models").json()
    assert body["kind"] == "demo"
    assert body["ok"] is True
    assert body["models"] == []


def test_api_token_is_required_when_configured(tmp_path, monkeypatch):
    from dataclasses import replace

    guarded = replace(make_settings(tmp_path, provider="demo"), api_token="test-token")
    monkeypatch.setattr(main_module, "settings", guarded)
    with TestClient(main_module.app) as guarded_client:
        assert guarded_client.get("/health").status_code == 200
        assert guarded_client.get("/api/config").status_code == 401
        assert (
            guarded_client.get(
                "/api/config", headers={"Authorization": "Bearer wrong"}
            ).status_code
            == 401
        )
        ok = guarded_client.get(
            "/api/config", headers={"Authorization": "Bearer test-token"}
        )
        assert ok.status_code == 200


def test_inline_run_completes_and_returns_trace(client):
    response = client.post(
        "/api/runs?wait=true",
        json={"goal": GOAL, "provider": "demo", "limits": {"max_agent_calls": 12}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["run"]["status"] == "completed"
    assert body["run"]["verification"]["passed"] is True
    assert len(body["events"]) > 5
    kinds = {event["kind"] for event in body["events"]}
    assert {"run_started", "routing_decision", "agent_invocation", "verification"} <= kinds

    run_id = body["run"]["run_id"]
    detail = client.get(f"/api/runs/{run_id}")
    assert detail.status_code == 200
    assert detail.json()["run"]["run_id"] == run_id

    events = client.get(f"/api/runs/{run_id}/events").json()
    assert events["run_id"] == run_id
    assert len(events["events"]) == len(body["events"])


def test_background_run_can_be_polled(client):
    response = client.post("/api/runs", json={"goal": GOAL, "provider": "demo"})
    assert response.status_code == 202
    run_id = response.json()["run"]["run_id"]
    assert response.json()["run"]["status"] in {"queued", "running"}

    deadline = time.time() + 30
    status = "queued"
    while time.time() < deadline:
        body = client.get(f"/api/runs/{run_id}").json()
        status = body["run"]["status"]
        if status in {"completed", "failed", "completed_with_findings", "budget_exceeded"}:
            break
        time.sleep(0.05)
    assert status == "completed"


def test_run_list_is_newest_first(client):
    first = client.post("/api/runs?wait=true", json={"goal": GOAL}).json()
    time.sleep(0.01)
    second = client.post("/api/runs?wait=true", json={"goal": GOAL + " again"}).json()

    runs = client.get("/api/runs").json()["runs"]
    ids = [run["run_id"] for run in runs]
    assert ids[0] == second["run"]["run_id"]
    assert first["run"]["run_id"] in ids


def test_validation_errors_are_rejected(client):
    response = client.post("/api/runs", json={"goal": "short"})
    assert response.status_code == 422


def test_unknown_run_is_404(client):
    assert client.get("/api/runs/run_missing").status_code == 404
    assert client.get("/api/runs/run_missing/events").status_code == 404


def test_openai_provider_without_config_is_a_400(client):
    response = client.post(
        "/api/runs?wait=true",
        json={"goal": GOAL, "provider": "openai"},
    )
    assert response.status_code == 400
    assert "OPENAI_COMPAT" in response.json()["detail"]