from __future__ import annotations

import httpx
import pytest

from app.providers.base import (
    ModelRequest,
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderConnectionError,
    ProviderNotFoundError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderServerError,
    ProviderTimeoutError,
)
from app.providers.openai_compat import OpenAICompatibleProvider

BASE = "http://127.0.0.1:11434/v1"


def request(model: str = "qwen2.5-coder") -> ModelRequest:
    return ModelRequest(
        role="coder",  # type: ignore[arg-type]
        system="system prompt",
        user='{"goal":"x"}',
        model=model,
    )


def provider_with(handler, **kwargs) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        BASE, "", 30, transport=httpx.MockTransport(handler), **kwargs
    )


def completion(content: str = '{"ok":true}', usage: dict | None = None) -> dict:
    body = {
        "model": "qwen2.5-coder",
        "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


async def test_success_reads_standard_usage_and_reports_mode():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json=completion(usage={"prompt_tokens": 12, "completion_tokens": 7}),
        )

    result = await provider_with(handler).complete(request())

    assert result.text == '{"ok":true}'
    assert result.input_tokens == 12
    assert result.output_tokens == 7
    assert result.usage_available is True
    assert result.structured_mode == "json_object"
    assert result.finish_reason == "stop"
    assert seen[0].url.path == "/v1/chat/completions"
    assert "authorization" not in {k.lower() for k in seen[0].headers}


async def test_api_key_is_sent_only_when_configured():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=completion())

    provider = OpenAICompatibleProvider(
        BASE, "local-key", 30, transport=httpx.MockTransport(handler)
    )
    await provider.complete(request())

    assert seen[0].headers["authorization"] == "Bearer local-key"


async def test_ollama_usage_variant_is_understood():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=completion(usage={"prompt_eval_count": 21, "eval_count": 9}),
        )

    result = await provider_with(handler).complete(request())

    assert (result.input_tokens, result.output_tokens) == (21, 9)
    assert result.usage_available is True


async def test_missing_usage_is_reported_not_invented():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=completion())

    result = await provider_with(handler).complete(request())

    assert (result.input_tokens, result.output_tokens) == (0, 0)
    assert result.usage_available is False


async def test_list_content_parts_are_joined():
    def handler(request: httpx.Request) -> httpx.Response:
        body = completion()
        body["choices"][0]["message"]["content"] = [
            {"type": "text", "text": '{"a":'},
            {"type": "text", "text": "1}"},
        ]
        return httpx.Response(200, json=body)

    result = await provider_with(handler).complete(request())
    assert result.text == '{"a":1}'


async def test_auth_error_is_classified_and_not_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid key"})

    with pytest.raises(ProviderAuthError) as info:
        await provider_with(handler).complete(request())

    assert info.value.category == "auth"
    assert info.value.retryable is False
    assert info.value.status_code == 401


async def test_rate_limit_carries_retry_after():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "slow down"}, headers={"retry-after": "2.5"})

    with pytest.raises(ProviderRateLimitError) as info:
        await provider_with(handler).complete(request())

    assert info.value.retryable is True
    assert info.value.retry_after_seconds == 2.5


async def test_not_found_points_at_model_and_url():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "model not found"})

    with pytest.raises(ProviderNotFoundError) as info:
        await provider_with(handler).complete(request())

    assert info.value.category == "not_found"
    assert "model id" in str(info.value)


async def test_server_error_is_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="overloaded")

    with pytest.raises(ProviderServerError) as info:
        await provider_with(handler).complete(request())

    assert info.value.retryable is True


async def test_json_mode_is_downgraded_once_when_rejected():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        payload = json.loads(request.content.decode())
        calls.append(payload)
        if "response_format" in payload:
            return httpx.Response(
                400,
                json={"error": "response_format is not supported by this server"},
            )
        return httpx.Response(200, json=completion())

    provider = provider_with(handler)
    result = await provider.complete(request())

    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert "response_format" not in calls[1]
    assert result.structured_mode == "prompt_only"


async def test_bad_request_without_json_hint_fails_fast():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, json={"error": "bad model parameter"})

    with pytest.raises(ProviderBadRequestError):
        await provider_with(handler).complete(request())

    assert len(calls) == 1


async def test_timeout_is_classified():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(ProviderTimeoutError) as info:
        await provider_with(handler).complete(request())

    assert info.value.retryable is True


async def test_connection_failure_is_classified():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ProviderConnectionError):
        await provider_with(handler).complete(request())


async def test_invalid_shape_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(ProviderResponseError):
        await provider_with(handler).complete(request())


async def test_empty_content_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        body = completion()
        body["choices"][0]["message"]["content"] = None
        return httpx.Response(200, json=body)

    with pytest.raises(ProviderResponseError):
        await provider_with(handler).complete(request())


async def test_non_json_body_is_reported():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    with pytest.raises(ProviderResponseError):
        await provider_with(handler).complete(request())


async def test_health_lists_models_without_inference():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"data": [{"id": "qwen2.5-coder"}, {"id": "llama3.2"}]})

    result = await provider_with(handler).health()

    assert result["ok"] is True
    assert result["models"] == ["qwen2.5-coder", "llama3.2"]
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/v1/models"


async def test_health_reports_unreachable_endpoint():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = await provider_with(handler).health()
    assert result["ok"] is False
    assert "connection" in str(result["detail"]).lower()


async def test_full_endpoint_url_is_not_double_suffixed():
    provider = OpenAICompatibleProvider(
        f"{BASE}/chat/completions",
        "",
        30,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=completion())),
    )
    assert provider.endpoint.endswith("/v1/chat/completions")
    assert provider.endpoint.count("/chat/completions") == 1


def test_missing_base_url_is_a_config_error():
    from app.providers.base import ProviderError

    with pytest.raises(ProviderError) as info:
        OpenAICompatibleProvider("", "")
    assert info.value.category == "config"


@pytest.mark.parametrize("role", ["planner", "coder", "reviewer"])
async def test_roles_use_the_same_wire_contract(role):
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.append(json.loads(request.content.decode()))
        return httpx.Response(200, json=completion())

    payload = ModelRequest(
        role=role,  # type: ignore[arg-type]
        system="s",
        user="u",
        model="m",
    )
    await provider_with(handler).complete(payload)

    assert captured[0]["messages"][0]["role"] == "system"
    assert captured[0]["messages"][1]["role"] == "user"
    assert captured[0]["temperature"] == 0.1