"""Agent base: every role has a typed input model, a prompt, and a typed output."""

from __future__ import annotations

import json
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from app.models import AgentRole
from app.providers.base import ModelRequest

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT", bound=BaseModel)


class AgentError(RuntimeError):
    pass


class AgentOutputError(AgentError):
    """The model responded, but not with a valid typed output."""


def extract_json_object(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        raw = "\n".join(lines).strip()
    try:
        data = json.loads(raw)
    except ValueError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise AgentOutputError("response contained no JSON object") from None
        try:
            data = json.loads(raw[start : end + 1])
        except ValueError as exc:
            raise AgentOutputError(f"response was not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentOutputError("response JSON must be an object")
    return data


class Agent(Generic[InputT, OutputT]):
    role: AgentRole
    input_model: type[InputT]
    output_model: type[OutputT]
    system: str

    def build_request(self, payload: InputT, model: str) -> ModelRequest:
        if not isinstance(payload, self.input_model):
            raise AgentError(
                f"{type(self).__name__} expected {self.input_model.__name__}, "
                f"got {type(payload).__name__}"
            )
        return ModelRequest(
            role=self.role,
            system=self.system,
            user=json.dumps(payload.model_dump(mode="json"), indent=2),
            model=model,
            json_mode=True,
        )

    def parse(self, text: str) -> OutputT:
        try:
            return self.output_model.model_validate(extract_json_object(text))
        except ValidationError as exc:
            raise AgentOutputError(f"typed output rejected: {exc.errors()[:3]}") from exc