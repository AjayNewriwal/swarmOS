from __future__ import annotations

from app.config import Settings
from app.providers.base import (
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderConfigError,
    ProviderError,
)
from app.providers.demo import DemoProvider
from app.providers.openai_compat import OpenAICompatibleProvider

__all__ = [
    "DemoProvider",
    "ModelRequest",
    "ModelResponse",
    "OpenAICompatibleProvider",
    "Provider",
    "ProviderConfigError",
    "ProviderError",
    "resolve_provider",
]


def resolve_provider(name: str, settings: Settings) -> tuple[Provider, str, str | None]:
    """Return ``(provider, provider_name, warning)``.

    ``auto`` uses a configured OpenAI-compatible endpoint when one exists and
    otherwise falls back to the demo fixture, with a warning that is surfaced
    in the run record and the trace.
    """

    choice = (name or "auto").strip().lower()
    if choice == "demo":
        return DemoProvider(), "demo", "Demo fixture: no model was called."
    if choice == "openai":
        if not settings.openai_configured:
            raise ProviderConfigError(
                "OPENAI_COMPAT_BASE_URL, OPENAI_COMPAT_API_KEY, and "
                "OPENAI_COMPAT_MODEL_FAST must be set to use the openai provider."
            )
        provider = OpenAICompatibleProvider(
            settings.base_url, settings.api_key, settings.request_timeout_seconds
        )
        return provider, "openai-compatible", None
    if settings.openai_configured:
        provider = OpenAICompatibleProvider(
            settings.base_url, settings.api_key, settings.request_timeout_seconds
        )
        return provider, "openai-compatible", None
    return DemoProvider(), "demo", "No model endpoint configured; using the demo fixture."