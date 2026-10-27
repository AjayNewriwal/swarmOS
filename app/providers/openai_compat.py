"""Adapter for any OpenAI-compatible chat-completions endpoint.

Targets local inference first: Ollama, vLLM, llama.cpp server, LM Studio, and
TGI all expose a compatible route. Hosted compatible APIs work too, but no
hosted provider is assumed to be free or reachable.

Design notes:

* an API key is optional, because local inference usually has none;
* HTTP failures are classified so the caller can retry only what is retryable;
* ``response_format`` is sent when configured, and downgraded once if the
  server rejects it, because support varies across compatible servers;
* usage is read from the standard fields and from Ollama's variant, and
  ``usage_available`` records whether any usage metadata was present. No token
  counts are invented.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.providers.base import (
    ModelRequest,
    ModelResponse,
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderConnectionError,
    ProviderError,
    ProviderNotFoundError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeoutError,
)


class OpenAICompatibleProvider:
    name = "openai-compatible"

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout_seconds: int = 90,
        *,
        json_mode: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url:
            raise ProviderError(
                "base_url is required",
                category="config",
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self._json_mode = json_mode
        self._transport = transport

    # ------------------------------------------------------------------
    # urls
    # ------------------------------------------------------------------
    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    @property
    def root(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url[: -len("/chat/completions")]
        return self.base_url

    @property
    def models_endpoint(self) -> str:
        return f"{self.root}/models"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        # Only send Authorization when a key exists; local servers reject or
        # ignore empty bearer tokens inconsistently.
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _client(self, timeout_seconds: int | None = None) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "timeout": timeout_seconds or self.timeout_seconds
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    # ------------------------------------------------------------------
    # inference
    # ------------------------------------------------------------------
    async def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        try:
            data = await self._post_with_downgrade(request)
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001 - unexpected transport failure
            raise ProviderError(
                f"unexpected provider failure: {type(exc).__name__}: {exc}",
                category="server",
                retryable=False,
            ) from exc

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        text, finish_reason = self._extract_content(data, request.model)
        input_tokens, output_tokens, usage_available = self._extract_usage(data)
        return ModelResponse(
            text=text,
            model=str(data.get("model") or request.model),
            provider=self.name,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=elapsed_ms,
            usage_available=usage_available,
            structured_mode="json_object" if self._json_mode else "prompt_only",
            finish_reason=finish_reason,
        )

    async def _post_with_downgrade(self, request: ModelRequest) -> dict[str, Any]:
        modes = [self._json_mode]
        if self._json_mode:
            modes.append(False)  # one downgrade attempt if the server rejects the field
        last_error: ProviderError | None = None

        for use_json in modes:
            payload: dict[str, Any] = {
                "model": request.model,
                "messages": [
                    {"role": "system", "content": request.system},
                    {"role": "user", "content": request.user},
                ],
                "temperature": 0.1,
            }
            if use_json and request.json_mode:
                payload["response_format"] = {"type": "json_object"}

            try:
                async with self._client() as client:
                    response = await client.post(
                        self.endpoint, headers=self._headers(), json=payload
                    )
            except httpx.TimeoutException as exc:
                raise ProviderTimeoutError(
                    f"provider timed out after {self.timeout_seconds}s"
                ) from exc
            except httpx.ConnectError as exc:
                raise ProviderConnectionError(
                    f"provider unreachable at {self.endpoint}: {exc}"
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderConnectionError(f"provider request failed: {exc}") from exc

            if response.status_code >= 400:
                error = self._classify(response)
                if (
                    use_json
                    and isinstance(error, ProviderBadRequestError)
                    and _mentions_json_mode(error)
                ):
                    last_error = error
                    self._json_mode = False
                    continue
                raise error

            try:
                data = response.json()
            except ValueError as exc:
                raise ProviderResponseError(
                    "provider returned a non-JSON body", status_code=response.status_code
                ) from exc
            if not isinstance(data, dict):
                raise ProviderResponseError("provider response must be a JSON object")
            return data

        raise last_error or ProviderBadRequestError("provider rejected the request")

    # ------------------------------------------------------------------
    # probe (no inference, no cost)
    # ------------------------------------------------------------------
    async def health(self) -> dict[str, object]:
        # A readiness check should fail fast; do not inherit a 90s inference
        # timeout for listing models.
        try:
            async with self._client(min(self.timeout_seconds, 10)) as client:
                response = await client.get(self.models_endpoint, headers=self._headers())
        except httpx.TimeoutException:
            return {"ok": False, "detail": "timed out listing models", "models": []}
        except httpx.HTTPError as exc:
            return {"ok": False, "detail": f"connection failed: {exc}", "models": []}
        if response.status_code >= 400:
            error = self._classify(response)
            return {
                "ok": False,
                "detail": f"{error.category}: {str(error)[:300]}",
                "models": [],
                "status_code": response.status_code,
            }
        try:
            data = response.json()
        except ValueError:
            return {"ok": False, "detail": "non-JSON model list", "models": []}
        entries = data.get("data") if isinstance(data, dict) else None
        models: list[str] = []
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict):
                    name = entry.get("id") or entry.get("name")
                    if isinstance(name, str):
                        models.append(name)
        return {"ok": True, "detail": "reachable", "models": models}

    # ------------------------------------------------------------------
    # parsing helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_content(data: dict[str, Any], model: str) -> tuple[str, str | None]:
        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(
                f"unexpected response shape (no choices[0].message): {exc}"
            ) from exc

        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    parts.append(part["text"])
            content = "".join(parts)
        if not isinstance(content, str):
            # Some reasoning models put nothing in content and only emit
            # reasoning_content; that is not a usable typed answer.
            raise ProviderResponseError(
                f"provider returned no text content for model {model!r}"
            )
        finish_reason = choice.get("finish_reason") if isinstance(choice, dict) else None
        return content, finish_reason if isinstance(finish_reason, str) else None

    @staticmethod
    def _extract_usage(data: dict[str, Any]) -> tuple[int, int, bool]:
        usage = data.get("usage")
        if not isinstance(usage, dict):
            return 0, 0, False
        prompt = usage.get("prompt_tokens", usage.get("prompt_eval_count"))
        completion = usage.get("completion_tokens", usage.get("eval_count"))
        # Only a complete pair is usable: reporting one half as zero would make a
        # partial usage block look measured and understate the cost.
        if not (isinstance(prompt, int) and isinstance(completion, int)):
            return 0, 0, False
        return prompt, completion, True

    @staticmethod
    def _classify(response: httpx.Response) -> ProviderError:
        body = response.text[:500] if response.text else ""
        status = response.status_code
        if status in {401, 403}:
            return ProviderAuthError(
                f"authentication failed (HTTP {status}): {body}",
                status_code=status,
            )
        if status == 429:
            return ProviderRateLimitError(
                f"rate limited (HTTP 429): {body}",
                status_code=status,
                retry_after_seconds=_retry_after(response),
            )
        if status == 404:
            return ProviderNotFoundError(
                f"not found (HTTP 404); check the base URL and model id: {body}",
                status_code=status,
            )
        if status == 408:
            return ProviderTimeoutError(
                f"provider timed out (HTTP 408): {body}", status_code=status
            )
        if status in {400, 422}:
            return ProviderBadRequestError(
                f"request rejected (HTTP {status}): {body}", status_code=status
            )
        if status >= 500:
            return ProviderServerError(
                f"provider error (HTTP {status}): {body}", status_code=status
            )
        return ProviderError(
            f"unexpected HTTP {status}: {body}", status_code=status, retryable=False
        )


def _mentions_json_mode(error: ProviderError) -> bool:
    text = str(error).lower()
    return "response_format" in text or "json_object" in text or "json mode" in text


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None