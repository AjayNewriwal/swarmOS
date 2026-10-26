from __future__ import annotations

from app.models import (
    FileChange,
    Limits,
    Plan,
    ReviewOutput,
    RunRecord,
    TraceEvent,
    VerificationResult,
)
from app.store import RunStore
from tests.conftest import PLAN_THREE_STEPS


def test_run_roundtrip_preserves_typed_fields(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    try:
        record = RunRecord(
            goal="do the thing",
            status="completed",
            plan=Plan.model_validate(PLAN_THREE_STEPS),
            review=ReviewOutput(verdict="approve", rationale="fine"),
            verification=VerificationResult(
                passed=True,
                command="python -m pytest -q",
                exit_code=0,
                duration_ms=12,
                tests_passed=3,
                workspace_files=["a.py"],
            ),
            changed_files=["a.py"],
            artifacts={"a.py": "x = 1\n"},
            limits=Limits(max_agent_calls=7, max_budget_usd=0.5),
            cost_usd=0.0123,
            agent_calls=4,
        )
        store.save_run(record)

        loaded = store.get_run(record.run_id)
        assert loaded is not None
        assert loaded.plan is not None and len(loaded.plan.steps) == 3
        assert loaded.review is not None and loaded.review.verdict == "approve"
        assert loaded.verification is not None and loaded.verification.tests_passed == 3
        assert loaded.changed_files == ["a.py"]
        assert loaded.artifacts == {"a.py": "x = 1\n"}
        assert loaded.limits.max_agent_calls == 7
    finally:
        store.close()


def test_save_run_upserts(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    try:
        record = RunRecord(goal="do the thing", status="queued")
        store.save_run(record)
        store.save_run(record.model_copy(update={"status": "completed"}))
        loaded = store.get_run(record.run_id)
        assert loaded is not None and loaded.status == "completed"
        assert len(store.list_runs()) == 1
    finally:
        store.close()


def test_events_are_ordered_by_sequence(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    try:
        record = RunRecord(goal="do the thing")
        store.save_run(record)
        for seq in (3, 1, 2):
            store.append_event(
                TraceEvent(run_id=record.run_id, seq=seq, kind="routing_decision")
            )
        events = store.get_events(record.run_id)
        assert [event.seq for event in events] == [1, 2, 3]
    finally:
        store.close()


def test_duplicate_event_ids_are_ignored(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    try:
        record = RunRecord(goal="do the thing")
        store.save_run(record)
        event = TraceEvent(run_id=record.run_id, seq=1, kind="run_started")
        store.append_event(event)
        store.append_event(event)
        assert len(store.get_events(record.run_id)) == 1
    finally:
        store.close()


def test_list_runs_respects_the_limit(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    try:
        for index in range(5):
            store.save_run(RunRecord(goal=f"goal number {index}"))
        assert len(store.list_runs(limit=2)) == 2
    finally:
        store.close()


def test_file_change_roundtrip_in_trace_payload(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    try:
        record = RunRecord(goal="do the thing")
        store.save_run(record)
        change = FileChange(path="a.py", content="x", step_id="alpha")
        store.append_event(
            TraceEvent(
                run_id=record.run_id,
                seq=1,
                kind="routing_decision",
                payload={"change": change},
            )
        )
        payload = store.get_events(record.run_id)[0].payload
        assert payload["change"]["step_id"] == "alpha"
    finally:
        store.close()