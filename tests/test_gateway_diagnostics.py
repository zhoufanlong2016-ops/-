from __future__ import annotations

from io import BytesIO
import json
import threading
from types import MethodType
from types import SimpleNamespace
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

    def fake_request_json(_endpoint, _key, payload, **_kwargs):
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


def test_structured_qwen_repair_calls_share_one_total_deadline(monkeypatch) -> None:
    import document_translator.services.translation_gateway as module

    deadlines: list[float | None] = []
    responses = iter([
        {"choices": [{"message": {"content": '[{"id":"road","output":"道路"}]'}}]},
        {"choices": [{"message": {"content": '[{"id":"road","output":"RAVI Rd. 道路"}]'}}]},
    ])

    def fake_request_json(_endpoint, _key, payload, *, deadline=None):
        deadlines.append(deadline)
        return next(responses)

    monkeypatch.setattr(module, "_request_json", fake_request_json)
    sent: list[tuple[int, object]] = []
    handler = object.__new__(_Handler)
    handler._audit_request = MethodType(lambda _self, _provider, _body: None, handler)
    handler._audit_result = MethodType(lambda _self, *_args, **_kwargs: None, handler)
    handler._send = MethodType(lambda _self, status, payload: sent.append((status, payload)), handler)
    config = GatewayConfig.from_provider(provider="qwen", model="qwen3.8-flash")
    prompt = "## Here is the input:\n" + json.dumps([{"id": "road", "input": "RAVI Rd."}])

    handler._handle_qwen(
        config,
        "secret",
        {
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        },
    )

    assert len(deadlines) == 2
    assert deadlines[0] == deadlines[1]
    assert sent[0][0] == 200


def test_local_validation_audit_records_only_rule_categories(tmp_path) -> None:
    audit_path = tmp_path / "gateway-events.jsonl"
    handler = object.__new__(_Handler)
    handler.gateway_request_id = "gateway-request-test"
    handler.server = SimpleNamespace(
        gateway_audit_path=str(audit_path), gateway_audit_lock=threading.Lock()
    )
    request_body = {
        "model": "qwen3.8-flash",
        "messages": [{"role": "user", "content": "confidential document text"}],
    }

    handler._audit_validation(
        "qwen",
        request_body,
        phase="initial",
        errors=[
            "row-1: PROPER_NAME_MISSING: PRIVATE_PLACE expected 1, got 0",
            "row-2: PLACEHOLDER_MISMATCH: [[SECRET_01]] expected 1, got 0",
        ],
    )

    record = json.loads(audit_path.read_text(encoding="utf-8"))
    assert record["gateway_request_id"] == "gateway-request-test"
    assert record["validator"] == "document_translator.translation_rules"
    assert record["decision_maker"] == "local_validation_rules"
    assert record["corrector"] == "selected_provider_model"
    assert record["corrector_model"] == "qwen3.8-flash"
    assert record["correction_action"] == "request_one_batch_correction"
    assert record["failure_categories"] == {
        "PROPER_NAME_MISSING": 1,
        "PLACEHOLDER_MISMATCH": 1,
    }
    assert "PRIVATE_PLACE" not in audit_path.read_text(encoding="utf-8")
    assert "SECRET_01" not in audit_path.read_text(encoding="utf-8")


def test_correction_call_is_identified_as_same_provider_correction() -> None:
    import document_translator.services.translation_gateway as module

    correction = {
        "messages": [
            {"role": "user", "content": "translate"},
            {"role": "system", "content": "AUTOMATIC CORRECTION: retain required names"},
        ]
    }
    assert module._request_phase(correction) == "correction"
    assert module._request_phase({"messages": [{"role": "user", "content": "translate"}]}) == "translation"


def test_upstream_request_fails_immediately_after_shared_deadline(monkeypatch) -> None:
    import document_translator.services.translation_gateway as module

    called = False

    def fake_urlopen(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("expired deadline must not start another network call")

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    try:
        module._request_json(
            "https://example.test", "secret", {"x": 1}, deadline=module.time.monotonic() - 1
        )
    except GatewayError as error:
        assert error.code == "UPSTREAM_DEADLINE_EXCEEDED"
        assert error.status == 504
    else:  # pragma: no cover - expired work must fail closed
        raise AssertionError("expected total-deadline failure")

    assert called is False


def test_upstream_socket_timeout_at_deadline_is_reported_as_deadline(monkeypatch) -> None:
    import document_translator.services.translation_gateway as module

    clock = [10.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(module, "_MAX_UPSTREAM_ATTEMPTS", 1)

    def fake_urlopen(_request, *, timeout):
        assert timeout == 1.0
        clock[0] = 11.0
        raise TimeoutError("socket timed out")

    monkeypatch.setattr(module, "urlopen", fake_urlopen)

    try:
        module._request_json(
            "https://example.test", "secret", {"x": 1}, deadline=11.0
        )
    except GatewayError as error:
        assert error.code == "UPSTREAM_DEADLINE_EXCEEDED"
        assert error.status == 504
    else:  # pragma: no cover - exhausting the deadline must be explicit
        raise AssertionError("expected total-deadline failure")


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

