from __future__ import annotations

from app.config import Settings
from app.providers.base import (
    ModelRequest,
    ModelResponse,
    ModelUnavailableError,
    ProbeableProvider,
    Provider,
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderConfigError,
    ProviderConnectionError,
    ProviderError,
    ProviderNotFoundError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeoutError,
)
from app.providers.demo import DemoProvider
from app.providers.openai_compat import OpenAICompatibleProvider

__all__ = [
    "DemoProvider",
    "ModelRequest",
    "ModelResponse",
    "ModelUnavailableError",
    "OpenAICompatibleProvider",
    "ProbeableProvider",
    "Provider",
    "ProviderAuthError",
    "ProviderBadRequestError",
    "ProviderConfigError",
    "ProviderConnectionError",
    "ProviderError",
    "ProviderNotFoundError",
    "ProviderRateLimitError",
    "ProviderResponseError",
    "ProviderServerError",
    "ProviderTimeoutError",
    "resolve_provider",
]


def resolve_provider(name: str, settings: Settings) -> tuple[Provider, str, str | None]:
    """Return ``(provider, provider_name, warning)``.

    ``auto`` uses a configured OpenAI-compatible endpoint when one exists and
    otherwise falls back to the demo fixture, with a warning that is surfaced
    in the run record and the trace.

    An API key is optional: local runtimes normally have none. Only a missing
    base URL or fast model makes ``openai`` unavailable.
    """

    choice = (name or "auto").strip().lower()
    if choice == "demo":
        return DemoProvider(), "demo", "Demo fixture: no model was called."
    if choice == "openai":
        if not settings.openai_configured:
            raise ProviderConfigError(
                "OPENAI_COMPAT_BASE_URL and OPENAI_COMPAT_MODEL_FAST must be set "
                "to use the openai provider. An API key is optional for local "
                "inference."
            )
        provider = OpenAICompatibleProvider(
            settings.base_url,
            settings.api_key,
            settings.request_timeout_seconds,
            json_mode=settings.json_mode,
        )
        kind = settings.provider_kind
        warning = None
        if not settings.api_key:
            warning = (
                "No API key configured. This is expected for local inference; "
                "hosted endpoints will reject it."
            )
        return provider, f"openai-compatible:{kind}", warning
    if settings.openai_configured:
        provider = OpenAICompatibleProvider(
            settings.base_url,
            settings.api_key,
            settings.request_timeout_seconds,
            json_mode=settings.json_mode,
        )
        return provider, f"openai-compatible:{settings.provider_kind}", None
    return DemoProvider(), "demo", "No model endpoint configured; using the demo fixture."