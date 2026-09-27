from __future__ import annotations

import json

import httpx
import pytest

from document_translator.core import DocumentFormat, DocumentLocation, TranslationUnit, generate_unit_id
from document_translator.providers.openai_api import OpenAIConfig, OpenAIProvider, OpenAIProviderError
from document_translator.services import Glossary, GlossaryEntry


def unit() -> TranslationUnit:
    data = dict(
        document_hash="a" * 64, format=DocumentFormat.DOCX,
        location=DocumentLocation(part="word/document.xml", object_id="w:p:0", node_ids=["w:t:0"]),
        source_language="zh-CN", target_language="en", source_text="原文", protected_tokens=[],
        style_signature="", context_before="", context_after="",
    )
    data["id"] = generate_unit_id(**data)
    return TranslationUnit.model_validate(data)


def test_responses_provider_extracts_output_text(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "Translation"}]}]})
    provider = OpenAIProvider(OpenAIConfig(model="gpt-5.6-luna"), client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = provider.translate_unit(unit())
    assert result.translation == "Translation"
    assert seen[0].url == httpx.URL("https://api.openai.com/v1/responses")
    payload = json.loads(seen[0].content)
    assert payload["model"] == "gpt-5.6-luna"
    assert "Engineering and contract terminology" in payload["input"]
    assert "isolated drawing labels or table headers" in payload["input"]


def test_responses_provider_injects_only_matching_glossary_terms(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    sent: list[httpx.Request] = []
    glossary = Glossary(
        entries=(
            GlossaryEntry(source="原文", target="Source text"),
            GlossaryEntry(source="unrelated", target="无关"),
        ),
        version="glossary-v1",
    )

    provider = OpenAIProvider(
        OpenAIConfig(model="gpt-5.6-luna"),
        client=httpx.Client(transport=httpx.MockTransport(
            lambda request: sent.append(request) or httpx.Response(
                200,
                json={"output": [{"type": "message", "content": [{"type": "output_text", "text": "Source text"}]}]},
            ),
        )),
        glossary=glossary,
    )

    result = provider.translate_unit(unit())

    payload = json.loads(sent[0].content)
    assert result.glossary_version == "glossary-v1"
    assert "- 原文 -> Source text" in payload["input"]
    assert "unrelated" not in payload["input"]


def test_responses_provider_rejects_missing_output(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    provider = OpenAIProvider(OpenAIConfig(model="gpt-5.6-terra"), client=httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"output": []}))))
    with pytest.raises(OpenAIProviderError, match="output text"):
        provider.translate_unit(unit())


def test_batch_glossary_failure_is_repaired_as_a_second_batch(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    glossary = Glossary(entries=(GlossaryEntry(source="原文", target="Source text"),), version="glossary-v1")
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        translation = "Translation" if len(calls) == 1 else "Source text"
        body = json.dumps({"items": [{"id": unit().id, "translation": translation}]}, ensure_ascii=False)
        return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": body}]}]}, request=request)

    provider = OpenAIProvider(
        OpenAIConfig(model="gpt-5.6-luna"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        glossary=glossary,
    )
    result = provider.translate_batch([unit()])[0]

    assert result.translation == "Source text"
    assert len(calls) == 2
    assert "AUTOMATIC CORRECTION" in calls[1].content.decode()


def test_batch_validation_failure_after_retry_is_marked_for_review(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    data = unit().model_dump()
    data.update(
        source_text="LINE X Y",
        source_language="en",
        target_language="zh-CN",
        protected_tokens=[],
    )
    data["id"] = generate_unit_id(**{key: value for key, value in data.items() if key not in {"id", "status"}})
    failing_unit = TranslationUnit.model_validate(data)
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.dumps({"items": [{"id": failing_unit.id, "translation": "LINE X Y"}]})
        return httpx.Response(
            200,
            json={"output": [{"type": "message", "content": [{"type": "output_text", "text": body}]}]},
            request=request,
        )

    result = OpenAIProvider(
        OpenAIConfig(model="gpt-5.6-luna"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    ).translate_batch([failing_unit])[0]

    assert result.validation_status == "needs_review"
    assert result.error is not None
    assert "UNTRANSLATED_ENGLISH" in result.error
    assert len(calls) == 2
