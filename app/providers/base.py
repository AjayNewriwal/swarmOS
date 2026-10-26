"""Provider contract.

SwarmOS never assumes that an assistant product exposes its internal models.
A provider is any object that can turn a typed prompt into text plus usage
numbers; the runtime owns pricing, retries, and accounting.

Errors carry a category and a retryability flag so the call runner can decide
between a bounded retry and an immediate, explicit failure. Treating a 401 like
a timeout is how a run burns money and time for no reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from app.models import AgentRole

ErrorCategory = Literal[
    "auth",
    "rate_limit",
    "timeout",
    "connection",
    "bad_request",
    "not_found",
    "server",
    "invalid_response",
    "unsupported",
    "config",
]


class ProviderError(RuntimeError):
    """Raised when a provider cannot produce a response."""

    category: ErrorCategory = "server"
    retryable: bool = False
    status_code: int | None = None

    def __init__(
        self,
        message: str,
        *,
        category: ErrorCategory | None = None,
        retryable: bool | None = None,
        status_code: int | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        if category is not None:
            self.category = category
        if retryable is not None:
            self.retryable = retryable
        if status_code is not None:
            self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds

    def describe(self) -> dict[str, object]:
        return {
            "category": self.category,
            "retryable": self.retryable,
            "status_code": self.status_code,
            "message": str(self)[:500],
        }


class ProviderConfigError(ProviderError):
    """Raised when a requested provider or model is not configured."""

    category: ErrorCategory = "config"
    retryable = False


class ProviderAuthError(ProviderError):
    category: ErrorCategory = "auth"
    retryable = False


class ProviderRateLimitError(ProviderError):
    category: ErrorCategory = "rate_limit"
    retryable = True


class ProviderTimeoutError(ProviderError):
    category: ErrorCategory = "timeout"
    retryable = True


class ProviderConnectionError(ProviderError):
    category: ErrorCategory = "connection"
    retryable = True


class ProviderBadRequestError(ProviderError):
    category: ErrorCategory = "bad_request"
    retryable = False


class ProviderNotFoundError(ProviderError):
    category: ErrorCategory = "not_found"
    retryable = False


class ProviderServerError(ProviderError):
    category: ErrorCategory = "server"
    retryable = True


class ProviderResponseError(ProviderError):
    category: ErrorCategory = "invalid_response"
    retryable = False


class ModelUnavailableError(ProviderConfigError):
    """The requested model tier is not configured, so fallback is impossible."""


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
    usage_available: bool = False
    structured_mode: str = "json_object"
    finish_reason: str | None = None


@runtime_checkable
class Provider(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Return a completion or raise ``ProviderError``."""
        ...


@runtime_checkable
class ProbeableProvider(Protocol):
    async def health(self) -> dict[str, object]:
        """Return ``{ok, detail, models}`` without running inference."""
        ...