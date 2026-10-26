"""SQLite persistence for runs and their append-only traces."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from app.models import Plan, ReviewOutput, RunRecord, TraceEvent, VerificationResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    goal              TEXT NOT NULL,
    provider          TEXT NOT NULL,
    provider_warning  TEXT,
    status            TEXT NOT NULL,
    limits_json       TEXT NOT NULL,
    plan_json         TEXT,
    review_json       TEXT,
    verification_json TEXT,
    summary           TEXT NOT NULL DEFAULT '',
    error             TEXT,
    cost_usd          REAL NOT NULL DEFAULT 0,
    agent_calls       INTEGER NOT NULL DEFAULT 0,
    escalations       INTEGER NOT NULL DEFAULT 0,
    revisions         INTEGER NOT NULL DEFAULT 0,
    duration_ms       INTEGER NOT NULL DEFAULT 0,
    changed_files_json TEXT NOT NULL DEFAULT '[]',
    artifacts_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS trace_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id     TEXT NOT NULL UNIQUE,
    run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    seq          INTEGER NOT NULL,
    at           TEXT NOT NULL,
    kind         TEXT NOT NULL,
    message      TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_trace_run ON trace_events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
"""


def _loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    return json.loads(value)


class RunStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path, check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    def save_run(self, record: RunRecord) -> None:
        data = record.model_dump(mode="json")
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO runs (
                    run_id, created_at, updated_at, goal, provider, provider_warning,
                    status, limits_json, plan_json, review_json, verification_json,
                    summary, error, cost_usd, agent_calls, escalations, revisions,
                    duration_ms, changed_files_json, artifacts_json
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(run_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    provider=excluded.provider,
                    provider_warning=excluded.provider_warning,
                    status=excluded.status,
                    plan_json=excluded.plan_json,
                    review_json=excluded.review_json,
                    verification_json=excluded.verification_json,
                    summary=excluded.summary,
                    error=excluded.error,
                    cost_usd=excluded.cost_usd,
                    agent_calls=excluded.agent_calls,
                    escalations=excluded.escalations,
                    revisions=excluded.revisions,
                    duration_ms=excluded.duration_ms,
                    changed_files_json=excluded.changed_files_json,
                    artifacts_json=excluded.artifacts_json
                """,
                (
                    data["run_id"],
                    data["created_at"],
                    data["updated_at"],
                    data["goal"],
                    data["provider"],
                    data["provider_warning"],
                    data["status"],
                    json.dumps(data["limits"]),
                    json.dumps(data["plan"]) if data["plan"] else None,
                    json.dumps(data["review"]) if data["review"] else None,
                    json.dumps(data["verification"]) if data["verification"] else None,
                    data["summary"],
                    data["error"],
                    data["cost_usd"],
                    data["agent_calls"],
                    data["escalations"],
                    data["revisions"],
                    data["duration_ms"],
                    json.dumps(data["changed_files"]),
                    json.dumps(data["artifacts"]),
                ),
            )
            self._conn.commit()

    def append_event(self, event: TraceEvent) -> None:
        data = event.model_dump(mode="json")
        with self._lock:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO trace_events
                    (event_id, run_id, seq, at, kind, message, payload_json)
                VALUES (?,?,?,?,?,?,?)
                """,
                (
                    data["event_id"],
                    data["run_id"],
                    data["seq"],
                    data["at"],
                    data["kind"],
                    data["message"],
                    json.dumps(data["payload"]),
                ),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    def get_run(self, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def list_runs(self, limit: int = 25) -> list[RunRecord]:
        limit = max(1, min(limit, 200))
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def get_events(self, run_id: str) -> list[TraceEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM trace_events WHERE run_id = ? ORDER BY seq ASC", (run_id,)
            ).fetchall()
        return [
            TraceEvent(
                event_id=row["event_id"],
                run_id=row["run_id"],
                seq=row["seq"],
                at=row["at"],
                kind=row["kind"],
                message=row["message"],
                payload=_loads(row["payload_json"], {}),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> RunRecord:
        plan = _loads(row["plan_json"])
        review = _loads(row["review_json"])
        verification = _loads(row["verification_json"])
        return RunRecord(
            run_id=row["run_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            goal=row["goal"],
            provider=row["provider"],
            provider_warning=row["provider_warning"],
            status=row["status"],
            limits=_loads(row["limits_json"], {}),
            plan=Plan.model_validate(plan) if plan else None,
            review=ReviewOutput.model_validate(review) if review else None,
            verification=VerificationResult.model_validate(verification)
            if verification
            else None,
            summary=row["summary"],
            error=row["error"],
            cost_usd=row["cost_usd"],
            agent_calls=row["agent_calls"],
            escalations=row["escalations"],
            revisions=row["revisions"],
            duration_ms=row["duration_ms"],
            changed_files=_loads(row["changed_files_json"], []),
            artifacts=_loads(row["artifacts_json"], {}),
        )