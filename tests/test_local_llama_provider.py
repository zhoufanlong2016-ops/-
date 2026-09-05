import json

import httpx
import pytest

from document_translator.core import DocumentFormat, DocumentLocation, TranslationUnit, generate_unit_id
from document_translator.providers.local_llama import (
    LocalLlamaConfig,
    LocalLlamaError,
    LocalLlamaProvider,
)
from document_translator.services import Glossary, GlossaryEntry


def make_unit() -> TranslationUnit:
    data = {
        "document_hash": "a" * 64,
        "format": DocumentFormat.MD,
        "location": DocumentLocation(part="source.md"),
        "source_language": "English",
        "target_language": "Chinese",
        "source_text": "Keep [[TOKEN_1]] safe.",
        "protected_tokens": ["[[TOKEN_1]]"],
    }
    return TranslationUnit(id=generate_unit_id(**data), **data)


def client_for(body: object, status_code: int = 200) -> tuple[httpx.Client, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status_code, json=body, request=request)

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


def success_body(unit: TranslationUnit, translation: str = "请保留 [[TOKEN_1]]。") -> dict:
    return {"choices": [{"message": {"content": json.dumps({"unit_id": unit.id, "translation": translation})}}]}


def test_translate_success_isolated_request_and_no_files(tmp_path) -> None:
    unit = make_unit()
    client, requests = client_for(success_body(unit))
    provider = LocalLlamaProvider(LocalLlamaConfig(model="qwen-local"), client=client)

    result = provider.translate_unit(unit)

    assert result.translation == "请保留 [[TOKEN_1]]。"
    assert result.request_count == 1
    assert result.provider == "local_llama"
    assert list(tmp_path.iterdir()) == []
    assert len(requests) == 1
    assert requests[0].url == "http://127.0.0.1:8088/v1/chat/completions"
    sent = requests[0].content.decode()
    assert "reference_translation" not in sent
    assert "English" in sent and "Chinese" in sent
    assert "Preserve every protected token exactly" in sent
    assert result.glossary_version == "none"


def test_plain_text_response_mode_binds_the_known_unit_id() -> None:
    unit = make_unit()
    client, requests = client_for({"choices": [{"message": {"content": "Keep [[TOKEN_1]] safe."}}]})

    result = LocalLlamaProvider(
        LocalLlamaConfig(model="hy-mt2", response_mode="plain_text"), client=client
    ).translate_unit(unit)

    assert result.unit_id == unit.id
    assert result.translation == "Keep [[TOKEN_1]] safe."
    prompt = requests[0].content.decode()
    assert "Translate the following text into Chinese" in prompt
    assert result.prompt_version == "hy-mt2-native-v2"
    payload = json.loads(prompt)
    assert payload["temperature"] == 0.7
    assert payload["top_p"] == 0.6
    assert payload["top_k"] == 20
    assert payload["repeat_penalty"] == 1.05
    assert "Return JSON only" not in prompt
    assert "<source>" not in prompt


def test_plain_text_response_rejects_prompt_leakage() -> None:
    unit = make_unit()
    client, _ = client_for({"choices": [{"message": {"content": "<source>prompt leakage</source>"}}]})

    with pytest.raises(LocalLlamaError) as caught:
        LocalLlamaProvider(
            LocalLlamaConfig(model="hy-mt2", response_mode="plain_text"), client=client
        ).translate_unit(unit)

    assert caught.value.code == "PROMPT_LEAKAGE"


def test_hy_prompt_maps_language_and_preserves_matching_glossary():
    unit = make_unit()
    data = unit.model_dump(exclude={"id"})
    data["target_language"] = "zh-CN"
    unit = TranslationUnit(id=generate_unit_id(**{k: v for k, v in data.items() if k != "status"}), **data)
    glossary = Glossary(entries=(GlossaryEntry(source="Keep", target="保留"),), version="terms-1")
    client, requests = client_for({"choices": [{"message": {"content": "保留 [[TOKEN_1]]。"}}]})
    result = LocalLlamaProvider(LocalLlamaConfig(response_mode="plain_text"), client=client, glossary=glossary).translate_unit(unit)
    prompt = json.loads(requests[0].content)["messages"][0]["content"]
    assert "into Chinese" in prompt
    assert "Keep translates to 保留" in prompt
    assert prompt.endswith(unit.source_text)
    assert unit.id not in prompt
    assert result.glossary_version == "terms-1"


@pytest.mark.parametrize("text,code", [("   ", "EMPTY_TRANSLATION"), ("译文未保留标记", "VALIDATION_FAILED")])
def test_hy_plain_text_still_rejects_empty_or_missing_tokens(text, code):
    client, _ = client_for({"choices": [{"message": {"content": text}}]})
    with pytest.raises(LocalLlamaError) as caught:
        LocalLlamaProvider(LocalLlamaConfig(response_mode="plain_text"), client=client).translate_unit(make_unit())
    assert caught.value.code == code


def test_glossary_injects_only_matching_terms_and_sets_result_version() -> None:
    unit = make_unit()
    glossary = Glossary(
        entries=(
            GlossaryEntry(source="Keep", target="保留"),
            GlossaryEntry(source="unrelated term", target="不相关术语"),
        ),
        version="glossary-v1",
    )
    client, requests = client_for(success_body(unit))

    result = LocalLlamaProvider(client=client, glossary=glossary).translate_unit(unit)

    sent = requests[0].content.decode()
    assert result.glossary_version == "glossary-v1"
    assert "Keep -> 保留" in sent
    assert "unrelated term -> 不相关术语" not in sent


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"choices": [{"message": {"content": "not json"}}]}, "MALFORMED_RESPONSE"),
        ({"choices": [{"message": {"content": json.dumps({"unit_id": "b" * 64, "translation": "ok [[TOKEN_1]]"})}}]}, "UNIT_ID_MISMATCH"),
        (None, "MALFORMED_RESPONSE"),
        ({"choices": [{"message": {}}]}, "MALFORMED_RESPONSE"),
    ],
)
def test_malformed_or_invalid_response_raises_provider_error(body, code) -> None:
    unit = make_unit()
    client, _ = client_for(body)
    with pytest.raises(LocalLlamaError) as caught:
        LocalLlamaProvider(client=client).translate_unit(unit)
    assert caught.value.code == code


@pytest.mark.parametrize(
    ("translation", "code"),
    [("   ", "EMPTY_TRANSLATION"), ("translated without token", "VALIDATION_FAILED")],
)
def test_empty_or_protected_token_mismatch_raises(translation, code) -> None:
    unit = make_unit()
    client, _ = client_for(success_body(unit, translation))
    with pytest.raises(LocalLlamaError) as caught:
        LocalLlamaProvider(client=client).translate_unit(unit)
    assert caught.value.code == code


def test_http_error_raises_provider_error() -> None:
    unit = make_unit()
    client, _ = client_for({"error": "offline"}, status_code=503)
    with pytest.raises(LocalLlamaError) as caught:
        LocalLlamaProvider(client=client).translate_unit(unit)
    assert caught.value.code == "HTTP_ERROR"


@pytest.mark.parametrize("kwargs", [{"model": ""}, {"timeout": 0}, {"response_mode": "xml"}])
def test_config_rejects_unsafe_values(kwargs) -> None:
    with pytest.raises(ValueError):
        LocalLlamaConfig(**kwargs)
