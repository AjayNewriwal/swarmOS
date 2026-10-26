"""Append-only trace of everything the orchestrator decides and observes."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.models import TraceEvent, TraceKind


def jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, set):
        return sorted(jsonable(item) for item in value)
    return value


class TraceRecorder:
    def __init__(self, run_id: str, store: Any = None) -> None:
        self.run_id = run_id
        self.store = store
        self.events: list[TraceEvent] = []
        self._seq = 0

    def emit(self, kind: TraceKind, message: str = "", **payload: Any) -> TraceEvent:
        self._seq += 1
        event = TraceEvent(
            run_id=self.run_id,
            seq=self._seq,
            kind=kind,
            message=message,
            payload=jsonable(payload),
        )
        self.events.append(event)
        if self.store is not None:
            self.store.append_event(event)
        return event