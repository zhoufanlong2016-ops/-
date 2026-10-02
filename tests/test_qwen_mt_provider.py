import json

import httpx
import pytest

from document_translator.core import DocumentFormat, DocumentLocation, TranslationUnit, generate_unit_id, sha256_text
from document_translator.providers import QwenMTConfig, QwenMTError, QwenMTProvider
from document_translator.services import Glossary, GlossaryEntry


def make_unit() -> TranslationUnit:
    data = {
        "document_hash": "a" * 64,
        "format": DocumentFormat.MD,
        "location": DocumentLocation(part="source.md"),
        "source_language": "English",
        "target_language": "Chinese",
        "source_text": "Install valve at K12+340: 600 mm to BS EN 752 [[TOKEN_1]].",
        "protected_tokens": ["[[TOKEN_1]]"],
    }
    return TranslationUnit(id=generate_unit_id(**data), **data)


def client_for(body: object, status_code: int = 200) -> tuple[httpx.Client, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json=body, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


def success_body(unit: TranslationUnit, translation: str | None = None) -> dict:
    translated = translation or "在 K12+340 按 BS EN 752 安装 600 mm 阀门 [[TOKEN_1]]。"
    return {"choices": [{"message": {"content": translated}}]}


def test_translate_success_uses_compatible_endpoint_auth_and_no_leakage(monkeypatch, tmp_path) -> None:
    unit = make_unit()
    client, requests = client_for(success_body(unit))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")

    result = QwenMTProvider(client=client).translate_unit(unit)

    assert result.translation.endswith("[[TOKEN_1]]。")
    assert result.request_count == 1
    assert result.provider == "qwen_mt"
    assert result.model == "qwen-mt-plus"
    assert len(requests) == 1
    assert str(requests[0].url) == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer test-secret"
    sent = requests[0].content.decode()
    assert "reference_translation" not in sent
    assert "translation_options" in sent
    assert "K12+340" in sent and "600 mm" in sent and "BS EN 752" in sent
    assert "Engineering and contract terminology" in sent
    payload = json.loads(sent)
    assert payload["messages"][0]["content"] == unit.source_text
    assert [message["role"] for message in payload["messages"]] == ["user"]
    assert payload["enable_thinking"] is False
    assert "max_tokens" not in payload
    assert payload["stream"] is False
    assert "Preserve numbers" in payload["translation_options"]["domains"]
    assert list(tmp_path.iterdir()) == []
    assert result.glossary_version == "none"


def test_glossary_injects_only_matching_terms_and_sets_result_version(monkeypatch) -> None:
    unit = make_unit()
    glossary = Glossary(
        entries=(
            GlossaryEntry(source="valve", target="阀门"),
            GlossaryEntry(source="unrelated term", target="不相关术语"),
        ),
        version="glossary-v1",
    )
    client, requests = client_for(success_body(unit))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")

    result = QwenMTProvider(client=client, glossary=glossary).translate_unit(unit)

    sent = requests[0].content.decode()
    assert result.glossary_version == "glossary-v1"
    assert '"source":"valve","target":"阀门"' in sent
    assert "unrelated term" not in sent


def test_rule_tokens_are_sent_as_official_qwen_verbatim_terms(monkeypatch) -> None:
    unit = make_unit().model_copy(update={
        "source_text": "Install 5% at 105+820.",
        "protected_tokens": ["5%", "105+820"],
    })
    unit = unit.model_copy(update={"id": generate_unit_id(**unit.model_dump(exclude={"id", "status"}))})
    client, requests = client_for({"choices": [{"message": {"content": "在 5% 处安装 105+820。"}}]})
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")

    result = QwenMTProvider(client=client).translate_unit(unit)

    assert result.translation == "在5%处安装105+820。"
    payload = json.loads(requests[0].content)
    assert {"source": "5%", "target": "5%"} in payload["translation_options"]["terms"]
    assert {"source": "105+820", "target": "105+820"} in payload["translation_options"]["terms"]


def test_validation_failure_repairs_only_source_gaps(monkeypatch) -> None:
    unit = make_unit().model_copy(update={
        "source_text": "The event occurred in 2015.", "protected_tokens": ["2015"],
    })
    unit = unit.model_copy(update={"id": generate_unit_id(**unit.model_dump(exclude={"id", "status"}))})
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        content = json.loads(request.content)["messages"][0]["content"]
        seen.append(content)
        reply = "事件发生在 202015 年。" if len(seen) == 1 else "事件发生在"
        return httpx.Response(200, json={"choices": [{"message": {"content": reply}}]}, request=request)

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    result = QwenMTProvider(client=httpx.Client(transport=httpx.MockTransport(handler))).translate_unit(unit)

    assert "2015" in result.translation
    assert "202015" not in result.translation
    assert seen == ["The event occurred in 2015.", "The event occurred in"]


@pytest.mark.parametrize("api_key", [None, "   "])
def test_missing_api_key_fails_before_request(monkeypatch, api_key: str | None) -> None:
    unit = make_unit()
    client, requests = client_for(success_body(unit))
    if api_key is None:
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    else:
        monkeypatch.setenv("DASHSCOPE_API_KEY", api_key)

    with pytest.raises(QwenMTError, match="not configured") as caught:
        QwenMTProvider(client=client).translate_unit(unit)

    assert caught.value.code == "API_KEY_MISSING"
    assert requests == []


@pytest.mark.parametrize("model", ["qwen-mt-lite", "qwen-mt-turbo", "", "QWEN-MT-PLUS"])
def test_config_rejects_lite_and_unknown_models(model: str) -> None:
    with pytest.raises(ValueError, match="qwen-mt-plus or qwen-mt-flash"):
        QwenMTConfig(model=model)


@pytest.mark.parametrize("body", [None, {"choices": []}])
def test_malformed_response_envelope_is_rejected(monkeypatch, body: object) -> None:
    unit = make_unit()
    client, _ = client_for(body)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")

    with pytest.raises(QwenMTError) as caught:
        QwenMTProvider(client=client).translate_unit(unit)

    assert caught.value.code == "MALFORMED_RESPONSE"


def test_malformed_api_json_and_multiple_choices_are_rejected(monkeypatch) -> None:
    unit = make_unit()
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")

    def invalid_json_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json", request=request)

    raw_client = httpx.Client(transport=httpx.MockTransport(invalid_json_handler))
    with pytest.raises(QwenMTError) as caught:
        QwenMTProvider(client=raw_client).translate_unit(unit)
    assert caught.value.code == "MALFORMED_RESPONSE"

    client, _ = client_for({"choices": [{"message": {"content": "{}"}}, {"message": {"content": "{}"}}]})
    with pytest.raises(QwenMTError) as caught:
        QwenMTProvider(client=client).translate_unit(unit)
    assert caught.value.code == "MALFORMED_RESPONSE"


@pytest.mark.parametrize(
    ("translation", "code"),
    [("   ", "EMPTY_TRANSLATION"), ("translated without token", "VALIDATION_FAILED")],
)
def test_empty_and_protected_token_mismatch_are_rejected(monkeypatch, translation: str, code: str) -> None:
    unit = make_unit()
    client, _ = client_for(success_body(unit, translation))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")

    with pytest.raises(QwenMTError) as caught:
        QwenMTProvider(client=client).translate_unit(unit)

    assert caught.value.code == code


def test_http_error_is_rejected_without_exposing_key(monkeypatch) -> None:
    unit = make_unit()
    client, _ = client_for({"error": "offline"}, status_code=503)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "do-not-expose")

    with pytest.raises(QwenMTError) as caught:
        QwenMTProvider(client=client).translate_unit(unit)

    assert caught.value.code == "HTTP_503"
    assert "do-not-expose" not in str(caught.value)


def test_configurable_api_key_environment_name(monkeypatch) -> None:
    unit = make_unit()
    client, requests = client_for(success_body(unit))
    monkeypatch.setenv("TEST_DASHSCOPE_KEY", "alternate-secret")

    QwenMTProvider(QwenMTConfig(api_key_env="TEST_DASHSCOPE_KEY"), client=client).translate_unit(unit)

    assert requests[0].headers["Authorization"] == "Bearer alternate-secret"


def test_batch_translates_full_original_units_under_shared_rate_gate(monkeypatch) -> None:
    def unit(index: int, text: str) -> TranslationUnit:
        data = {
            "document_hash": "b" * 64,
            "format": DocumentFormat.DWG,
            "location": DocumentLocation(part="Model", object_id=f"{index:X}"),
            "source_language": "Chinese",
            "target_language": "English",
            "source_text": text,
            "protected_tokens": [],
        }
        return TranslationUnit(id=generate_unit_id(**data), **data)

    units = [unit(1, "第一项"), unit(2, "第二项")]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "[[TRB:000000]]\nFirst\n[[/TRB:000000]]\n\n[[TRB:000001]]\nSecond\n[[/TRB:000001]]"}}]}, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")

    results = QwenMTProvider(client=client).translate_batch(units)

    assert [item.translation for item in results] == ["First", "Second"]
    assert [item.source_hash for item in results] == [sha256_text(unit.source_text) for unit in units]
    assert len(calls) == 1
    assert "[[TRB:000000]]" in calls[0]["messages"][0]["content"]


def test_batch_glossary_failure_is_repaired_as_a_second_batch(monkeypatch) -> None:
    unit = make_unit()
    glossary = Glossary(entries=(GlossaryEntry(source="valve", target="阀门"),), version="glossary-v1")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        text = "在 K12+340 按 BS EN 752 安装 600 mm 设备 [[TOKEN_1]]。"
        if len(calls) == 2:
            text = "在 K12+340 按 BS EN 752 安装 600 mm 阀门 [[TOKEN_1]]。"
        return httpx.Response(200, json={"choices": [{"message": {"content": f"[[TRB:000000]]\n{text}\n[[/TRB:000000]]"}}]}, request=request)

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    provider = QwenMTProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)), glossary=glossary,
    )
    result = provider.translate_batch([unit])[0]

    assert result.translation.endswith("阀门[[TOKEN_1]]。")
    assert len(calls) == 2
    for call in calls:
        assert {tuple(item.values()) for item in call["translation_options"]["terms"]} >= {
            ("valve", "阀门"), ("[[TOKEN_1]]", "[[TOKEN_1]]"),
        }
    assert "AUTOMATIC CORRECTION" in calls[1]["messages"][0]["content"]


def test_batch_validation_failure_after_retry_is_marked_for_review(monkeypatch) -> None:
    unit = make_unit()
    glossary = Glossary(entries=(GlossaryEntry(source="valve", target="闃€闂?"),), version="glossary-v1")
    calls = []
    text = "鍦?K12+340 鎸?BS EN 752 瀹夎 600 mm 璁惧 [[TOKEN_1]]銆?"

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": f"[[TRB:000000]]\n{text}\n[[/TRB:000000]]"}}]},
            request=request,
        )

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    result = QwenMTProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)), glossary=glossary,
    ).translate_batch([unit])[0]

    assert result.validation_status == "needs_review"
    assert result.error is not None
    assert "GLOSSARY_TERM_MISSING" in result.error
    assert len(calls) == 2


def test_single_unit_batch_accepts_raw_qwen_translation(monkeypatch) -> None:
    unit = make_unit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "直接译文 [[TOKEN_1]]。"}}]},
            request=request,
        )

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    provider = QwenMTProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = provider.translate_batch([unit])[0]

    assert result.translation == "直接译文[[TOKEN_1]]。"
