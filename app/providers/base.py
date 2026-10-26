"""Provider contract.

SwarmOS never assumes that an assistant product exposes its internal models.
A provider is any object that can turn a typed prompt into text plus usage
numbers; the runtime owns pricing, retries, and accounting.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.models import AgentRole


class ProviderError(RuntimeError):
    """Raised when a provider cannot produce a response."""


class ProviderConfigError(ProviderError):
    """Raised when a requested provider is not configured."""


@dataclass(frozen=True)
class ModelRequest:
    role: AgentRole
    system: str
    user: str
    model: str
    json_mode: bool = True


@dataclass(frozen=True)
class ModelResponse:
    text: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


@runtime_checkable
class Provider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Return a completion or raise ``ProviderError``."""
        ...