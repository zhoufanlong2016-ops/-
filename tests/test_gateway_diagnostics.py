from __future__ import annotations

from io import BytesIO
from types import MethodType
from urllib.error import HTTPError

from document_translator.services.translation_gateway import (
    GatewayConfig,
    GatewayError,
    _Handler,
    _request_json,
    _safe_upstream_diagnostics,
)


def test_upstream_billing_code_is_exposed_without_provider_body(monkeypatch) -> None:
    import document_translator.services.translation_gateway as module

    def fake_urlopen(_request, *, timeout):
        _ = timeout
        raise HTTPError(
            "https://example.test",
            400,
            "bad request",
            {"x-request-id": "request-123"},
            BytesIO(b'{"code":"Arrearage","message":"account details"}'),
        )

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    try:
        _request_json("https://example.test", "secret", {"x": 1})
    except GatewayError as error:
        assert error.code == "UPSTREAM_HTTP_400"
        assert error.upstream_code == "Arrearage"
        assert error.request_id == "request-123"
        assert "account details" not in str(error)
    else:  # pragma: no cover - the request must fail in this test
        raise AssertionError("expected GatewayError")


def test_nested_upstream_diagnostics_are_allowlisted() -> None:
    error = HTTPError(
        "https://example.test",
        403,
        "forbidden",
        {},
        BytesIO(b'{"error":{"code":"AllocationQuota.FreeTierOnly","message":"secret"}}'),
    )

    assert _safe_upstream_diagnostics(error) == ("AllocationQuota.FreeTierOnly", None)


def test_qwen_gateway_disables_thinking_and_streaming(monkeypatch) -> None:
    import document_translator.services.translation_gateway as module

    captured: dict[str, object] = {}

    def fake_request_json(_endpoint, _key, payload):
        captured.update(payload)
        return {"choices": [{"message": {"content": "done"}}]}

    monkeypatch.setattr(module, "_request_json", fake_request_json)
    handler = object.__new__(_Handler)
    handler._audit_request = MethodType(lambda _self, _provider, _body: None, handler)
    handler._send = MethodType(lambda _self, _status, _payload: None, handler)
    config = GatewayConfig.from_provider(provider="qwen", model="qwen3.8-max")

    handler._handle_qwen(
        config,
        "secret",
        {"messages": [{"role": "user", "content": "translate"}], "stream": True},
    )

    assert captured["model"] == "qwen3.8-max"
    assert captured["enable_thinking"] is False
    assert captured["reasoning_effort"] == "none"
    assert captured["stream"] is False


def test_qwen_gateway_rejects_non_chat_upstream_body(monkeypatch) -> None:
    import document_translator.services.translation_gateway as module

    monkeypatch.setattr(module, "_request_json", lambda *_args, **_kwargs: {"output": "plain"})
    sent: list[tuple[int, object]] = []
    handler = object.__new__(_Handler)
    handler._audit_request = MethodType(lambda _self, _provider, _body: None, handler)
    handler._send = MethodType(lambda _self, status, payload: sent.append((status, payload)), handler)
    config = GatewayConfig.from_provider(provider="qwen", model="qwen3.7-plus")

    try:
        handler._handle_qwen(
            config,
            "secret",
            {"messages": [{"role": "user", "content": "translate"}]},
        )
    except GatewayError as error:
        assert error.code == "UPSTREAM_INVALID_RESPONSE"
    else:  # pragma: no cover - malformed upstream bodies must fail closed
        raise AssertionError("expected GatewayError")
    assert sent == []


def test_pdf_worker_removes_babeldoc_unbounded_rate_limit_retry() -> None:
    from babeldoc.translator.translator import OpenAITranslator
    from document_translator.pdf_worker import _disable_nested_provider_retries

    _disable_nested_provider_retries(OpenAITranslator)

    for name in ("do_translate", "do_llm_translate"):
        method = getattr(OpenAITranslator, name)
        assert getattr(method, "_document_translator_single_attempt", False) is True
        assert not hasattr(method, "__wrapped__")
