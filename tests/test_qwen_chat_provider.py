import json

import httpx

from document_translator.core import DocumentFormat, DocumentLocation, TranslationUnit, generate_unit_id
from document_translator.providers import QwenChatConfig, QwenChatProvider
from document_translator.services import Glossary, GlossaryEntry


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


def test_qwen_chat_sends_json_batch_and_terms(monkeypatch) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        item = json.loads(body["messages"][1]["content"])["items"][0]
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"items": [{"id": item["id"], "translation": "阀门长 [[TRP_0000]]。"}]}, ensure_ascii=False)}}]},
            request=request,
        )

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    glossary = Glossary(entries=(GlossaryEntry(source="valve", target="阀门"),), version="v1")
    provider = QwenChatProvider(QwenChatConfig(model="qwen-plus"), client=httpx.Client(transport=httpx.MockTransport(handler)), glossary=glossary)
    result = provider.translate_batch([make_unit()])[0]

    assert result.translation == "阀门长2.4 m。"
    assert calls[0]["model"] == "qwen-plus"
    assert calls[0]["enable_thinking"] is False
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "valve -> 阀门" in calls[0]["messages"][0]["content"]


def test_qwen_chat_accepts_qwen_max_and_rejects_mt(monkeypatch) -> None:
    assert QwenChatConfig(model="qwen-max").model == "qwen-max"
    try:
        QwenChatConfig(model="qwen-mt-plus")
    except ValueError:
        pass
    else:
        raise AssertionError("Qwen-MT models must use QwenMTProvider")


def test_qwen_chat_marks_persistent_validation_failure_for_review(monkeypatch) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = json.loads(request.content)["messages"][1]
        item_id = json.loads(item["content"])["items"][0]["id"]
        body = {"items": [{"id": item_id, "translation": "LINE X Y"}]}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(body)}}]},
            request=request,
        )

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-secret")
    data = make_unit().model_dump()
    data["source_text"] = "LINE X Y"
    data["protected_tokens"] = []
    data["id"] = generate_unit_id(**{key: value for key, value in data.items() if key not in {"id", "status"}})
    result = QwenChatProvider(
        QwenChatConfig(model="qwen-plus"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).translate_batch([TranslationUnit.model_validate(data)])[0]

    assert result.validation_status == "needs_review"
    assert result.error is not None
    assert "UNTRANSLATED_ENGLISH" in result.error
    assert len(calls) == 2
