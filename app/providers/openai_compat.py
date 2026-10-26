"""Adapter for any OpenAI-compatible chat-completions endpoint.

This is transport only. It is intentionally generic because the host
environment may not expose a public model API at all; when nothing is
configured SwarmOS uses the deterministic demo provider instead.
"""

from __future__ import annotations

import time

import httpx

from app.providers.base import ModelRequest, ModelResponse, ProviderError


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(self, base_url: str, api_key: str, timeout_seconds: int = 90) -> None:
        if not base_url or not api_key:
            raise ProviderError("base_url and api_key are required")
        self.base_url = base_url
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload: dict[str, object] = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "temperature": 0.1,
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}

        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:500] if exc.response is not None else str(exc)
            raise ProviderError(f"provider returned HTTP {exc.response.status_code}: {detail}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(f"provider request failed: {exc}") from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected provider response shape: {exc}") from exc

        usage = data.get("usage") or {}
        return ModelResponse(
            text=str(text or ""),
            model=str(data.get("model") or request.model),
            provider=self.name,
            input_tokens=int(usage.get("prompt_tokens", 0) or 0),
            output_tokens=int(usage.get("completion_tokens", 0) or 0),
            latency_ms=elapsed_ms,
        )