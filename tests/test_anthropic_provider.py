import json

import httpx
import pytest

from document_translator.core import DocumentFormat, DocumentLocation, TranslationUnit, generate_unit_id
from document_translator.providers import AnthropicConfig, AnthropicError, AnthropicProvider


def make_unit() -> TranslationUnit:
    data = {
        "document_hash": "a" * 64,
        "format": DocumentFormat.MD,
        "location": DocumentLocation(part="markdown", object_id="line:1"),
        "source_language": "en",
        "target_language": "zh-CN",
        "source_text": "The valve is 2.4 m long.",
        "protected_tokens": ["2.4 m"],
    }
    return TranslationUnit(id=generate_unit_id(**data), **data)


def reply(request: httpx.Request, text: str, stop_reason: str = "end_turn") -> httpx.Response:
    return httpx.Response(
        200,
        json={"content": [{"type": "text", "text": text}], "stop_reason": stop_reason},
        request=request,
    )


def test_claude_sends_messages_request_and_parses_text_blocks(monkeypatch) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append((request, body))
        item = json.loads(body["messages"][0]["content"])["items"][0]
        text = json.dumps({"items": [{"id": item["id"], "translation": "阀门长 [[TRP_0000]]。"}]}, ensure_ascii=False)
        return reply(request, f"```json\n{text}\n```")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret")
    provider = AnthropicProvider(AnthropicConfig(model="claude-sonnet-5-5"), client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = provider.translate_batch([make_unit()])[0]

    request, body = calls[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "test-secret"
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers
    assert body["model"] == "claude-sonnet-5-5"
    assert body["max_tokens"] > 0
    assert "JSON" in body["system"]
    assert "response_format" not in body
    assert result.translation == "阀门长2.4m。"
    assert result.provider == "claude"


def test_claude_accepts_only_the_three_allowed_models() -> None:
    for model in ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"):
        assert AnthropicConfig(model=model).model == model
    with pytest.raises(ValueError):
        AnthropicConfig(model="claude-nonexistent")


def test_claude_reports_output_truncation_as_a_batch_failure(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return reply(request, '{"items": [', stop_reason="max_tokens")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-secret")
    provider = AnthropicProvider(AnthropicConfig(model="claude-haiku-5-5"), client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(AnthropicError) as raised:
        provider.translate_batch([make_unit()])
    assert raised.value.code == "BATCH_FAILED"


def test_claude_missing_key_is_reported_without_a_request(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    provider = AnthropicProvider(AnthropicConfig(model="claude-opus-5-5"), client=httpx.Client(transport=httpx.MockTransport(lambda request: 1 / 0)))
    with pytest.raises(AnthropicError) as raised:
        provider.translate_batch([make_unit()])
    assert raised.value.code == "API_KEY_MISSING"
