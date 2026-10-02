import pytest

from document_translator.translation_rules import (
    protect_for_translation,
    restore_after_translation,
    rule_protected_tokens,
)
from document_translator.core.validation import validate_placeholders
from document_translator.translation_rules import proper_names, validate_name_retention, validate_translation_residue, auto_correct_translation


def test_relative_path_is_not_extracted_from_drawing_review_label() -> None:
    assert rule_protected_tokens("CHD./VER.") == []
    assert rule_protected_tokens("Read ./docs/file.txt and ../assets/font.ttf") == ["./docs/file.txt", "../assets/font.ttf"]


def test_rule_protected_tokens_keeps_engineering_identifiers_and_bindings() -> None:
    text = (
        "At chainage 105+820–164+600, install Φ200 pipe at 1:200, "
        "USD 1,250.50, 5%, ISO 9001 and CCECC. See https://example.test/a."
    )

    assert rule_protected_tokens(text) == [
        "105+820–164+600",
        "Φ200",
        "1:200",
        "USD",
        "1,250.50",
        "5%",
        "ISO 9001",
        "CCECC",
        "https://example.test/a.",
    ]


def test_rule_protected_tokens_retains_existing_markdown_placeholders() -> None:
    assert rule_protected_tokens("Cost is 10%: [[TOKEN_0]]", ["[[TOKEN_0]]"]) == [
        "[[TOKEN_0]]", "10%",
    ]


def test_rule_protected_tokens_does_not_mistake_the_word_to_for_tonne_unit() -> None:
    assert rule_protected_tokens("From 2012 to 2019") == ["2012", "2019"]


def test_rule_protected_tokens_allows_ordinary_all_caps_heading_to_translate() -> None:
    assert rule_protected_tokens("TOTAL TENDER PRICE (A+B)") == []
    assert rule_protected_tokens("SCADA System Works") == ["SCADA"]


def test_rule_protected_tokens_keeps_multilevel_list_numbers_intact() -> None:
    assert rule_protected_tokens("2.1.1 Inception Report; 2.10.1 Final Report") == ["2.1.1", "2.10.1"]


def test_multilevel_list_number_may_touch_source_word() -> None:
    assert validate_placeholders("2.2.1Design Review", "2.2.1 设计审查", ["2.2.1"]) == []


def test_rule_protected_tokens_keeps_number_with_electrical_unit() -> None:
    assert rule_protected_tokens("450kW光伏系统") == ["450kW"]


def test_translation_protection_round_trips_numbers_but_keeps_markdown_marker() -> None:
    protected = protect_for_translation("At 105+820, use 5% and ⟦MD_0001⟧", ["105+820", "5%", "⟦MD_0001⟧"])

    assert protected.text == "At [[TRP_0000]], use [[TRP_0001]] and ⟦MD_0001⟧"
    assert restore_after_translation("在 [[TRP_0000]] 处使用 [[TRP_0001]] 和 ⟦MD_0001⟧", protected) == (
        "在 105+820 处使用 5% 和 ⟦MD_0001⟧"
    )
    with pytest.raises(ValueError, match="not preserved"):
        restore_after_translation("在此处使用。", protected)


@pytest.mark.parametrize("source,name", [
    ("RAVI Rd.", "RAVI Rd."),
    ("RIVER RAVI", "RIVER RAVI"), ("River Indus", "River Indus"),
    ("Lake Victoria", "Lake Victoria"), ("LAKE TANGANYIKA", "LAKE TANGANYIKA"),
    ("SHADMAN DS", "SHADMAN"), ("GARDEN HEIGHTS DS", "GARDEN HEIGHTS"),
    ("N-55 Highway", "N-55 Highway"), ("A12 HIGHWAY", "A12 HIGHWAY"),
    ("SHAREEF COLONY DS", "SHAREEF COLONY"),
    ("ZAFAR ALI ROAD DS", "ZAFAR ALI ROAD"),
    ("at Ravi Bridge", "Ravi Bridge"), ("Model Town", "Model Town"),
    ("Gulshan-e-Iqbal", "Gulshan-e-Iqbal"),
    ("Gulshan e Ravi", "Gulshan e Ravi"),
    ("along Ferozepur Highway", "Ferozepur Highway"),
    ("Davis Street", "Davis Street"),
])
def test_explicit_names_require_original_english(source, name):
    assert proper_names(source) == [name]
    assert validate_name_retention(source, "中文名称")
    assert validate_name_retention(source, f"中文名称（{name}）") == []
    assert validate_name_retention(source, f"中文名称（{name.swapcase()}）")


@pytest.mark.parametrize("heading", [
    "TOTAL TENDER PRICE", "ROAD CONSTRUCTION", "Main Road", "ACCESS ROAD",
    "Residential Colony", "Town Planning", "HIGHWAY DESIGN", "Ordinary heading",
    "The Contractor shall repair the road", "Complete road construction", "Completed road work",
    "Complete Road Construction", "COMPLETE ROAD CONSTRUCTION", "ravi road",
    "RIVER CROSSING", "RIVER TRAINING", "LAKE LEVEL", "TEMPORARY ROAD",
    "The DS shall be inspected", "INSPECT SHADMAN DS BEFORE WORK",
    "CONNECT DS", "the DS", "SHADMAN DS is downstream",
])
def test_ordinary_headings_are_not_proper_names(heading):
    assert proper_names(heading) == []
    assert validate_name_retention(heading, "普通标题") == []


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_geographic_names_do_not_absorb_adjacent_drawing_labels(newline):
    source = newline.join(["RIVER RAVI", "SHADMAN DS", "N-55 Highway"])
    assert proper_names(source) == ["RIVER RAVI", "SHADMAN", "N-55 Highway"]


def test_names_are_route_specific_and_cannot_hide_in_longer_word():
    assert validate_name_retention("Ravi Road", "新路", "zh", "en") == []
    assert validate_name_retention("Ravi Road", "新路", "en", "fr") == []
    assert validate_name_retention("Ravi Road", "Ravi Roadway")
    assert validate_name_retention("Ravi Road and Ravi Road", "Ravi Road")
    assert validate_name_retention("Ravi Road", "拉维路 (Ravi\nRoad)", "auto", "zh-CN") == []


@pytest.mark.parametrize(
    "source,translation,expected",
    [
        ("AREA= 11 ACRE", "面积=11英亩", []),
        ("AREA= 11 ACRE", "AREA= 11 ACRE", ["UNTRANSLATED_ENGLISH"]),
        ("LINE C", "C线", []),
        ("LINE C", "LINE C", ["UNTRANSLATED_ENGLISH"]),
        ("17th December 2025", "17日 2025年12月", []),
        ("17th December 2025", "17th2025年12月", ["DATE_ORDINAL_UNTRANSLATED"]),
        ("RAVI Rd.", "RAVI Rd.", []),
        ("This is a long engineering paragraph with many source words that are expected to be translated by the paragraph engine", "This remains in the paragraph", []),
    ],
)
def test_translation_residue_detects_short_labels_and_dates(source, translation, expected):
    errors = validate_translation_residue(source, translation, "en", "zh-CN")
    assert all(any(marker in error for error in errors) for marker in expected)
    if not expected:
        assert errors == []


def test_auto_corrects_deterministic_labels_and_ordinal_dates():
    assert auto_correct_translation("AREA= 11 ACRE", "AREA= 11 ACRE", "en", "zh-CN") == "面积= 11 英亩"
    assert auto_correct_translation("LINE C", "LINE C", "en", "zh-CN") == "C线"
    assert auto_correct_translation("17th December 2025", "17th 2025年12月", "en", "zh-CN") == "2025年12月17日"
    assert auto_correct_translation("17th December 2025", "17th2025年12月", "en", "zh-CN") == "2025年12月17日"
    assert auto_correct_translation("3rd Set of Clarifications", "3rd 澄清文件集", "en", "zh-CN") == "3rd 澄清文件集"
    assert auto_correct_translation("December, 2025", "12月，2025", "en", "zh-CN") == "2025年12月"
    assert auto_correct_translation("17th December 2025", "2025 12 17", "en", "zh-CN") == "2025年12月17日"
    assert auto_correct_translation("17th December 2025", "17 12 2025", "en", "zh-CN") == "2025年12月17日"


def _named_unit(format_name="md", text="RAVI Rd."):
    from document_translator.core import DocumentFormat, DocumentLocation, TranslationUnit, generate_unit_id
    data = dict(document_hash="a" * 64, format=DocumentFormat(format_name),
                location=DocumentLocation(part="test", object_id=text),
                source_text=text, source_language="en", target_language="zh-CN", protected_tokens=[])
    return TranslationUnit(id=generate_unit_id(**data), **data)


def _named_result(unit, text):
    from document_translator.core import TranslationResult, sha256_text
    return TranslationResult(unit_id=unit.id, translation=text, provider="openai", model="fake",
                             prompt_version="test", glossary_version="none", source_hash=sha256_text(unit.source_text),
                             result_hash=sha256_text(text), request_count=1, validation_status="valid")


@pytest.mark.parametrize("format_name", ["md", "docx", "xlsx", "pptx", "dwg", "pdf"])
def test_six_production_result_boundaries_reject_name_loss(format_name):
    from types import SimpleNamespace
    unit = _named_unit(format_name)
    result = _named_result(unit, "拉维路")
    provider = SimpleNamespace(provider_name="openai", config=SimpleNamespace(model="fake"),
                               prompt_version="test", glossary_version="none", translate_batch=lambda units: [result])
    if format_name == "md":
        from document_translator.services.markdown_translation import MarkdownTranslationService
        run = lambda: MarkdownTranslationService(provider)._validate_provider_result(unit, result)
    elif format_name == "docx":
        from document_translator.services.docx_translation import DocxTranslationService
        run = lambda: DocxTranslationService(provider)._translate_units((unit,))
    elif format_name == "xlsx":
        from document_translator.services.xlsx_translation import XlsxTranslationService
        run = lambda: XlsxTranslationService(provider)._translate_units((unit,))
    elif format_name == "dwg":
        from document_translator.services.dwg_translation import DwgTranslationService
        run = lambda: DwgTranslationService(provider)._validate_provider_result(unit, result)
    elif format_name == "pptx":
        from document_translator.services.pptx_translation import PptxTranslationService
        run = lambda: PptxTranslationService(provider)._apply_batch([(None, [], unit)])
    else:
        import json
        from document_translator.services.translation_gateway import _structured_answer_with_retry
        request = {"messages": [{"role": "user", "content": '## Here is the input:\n' + json.dumps([{"id": unit.id, "input": unit.source_text}])}], "response_format": {"type": "json_object"}}
        run = lambda: _structured_answer_with_retry(request, lambda body: json.dumps([{"id": unit.id, "output": "拉维路"}]))
    if format_name in {"docx", "xlsx", "pptx"}:
        # Kept and reported (like PDF) instead of discarding the whole document.
        service = {"docx": "DocxTranslationService", "xlsx": "XlsxTranslationService", "pptx": "PptxTranslationService"}[format_name]
        module = __import__(f"document_translator.services.{format_name}_translation", fromlist=[service])
        instance = getattr(module, service)(provider)
        if format_name == "pptx":
            with pytest.raises(IndexError):  # the fake paragraph has no text node: reached the write step
                instance._apply_batch([(None, [], unit)])
        else:
            instance._translate_units((unit,))
        assert any("PROPER_NAME_MISSING" in error for warning in instance.warnings for error in warning["errors"])
        return
    with pytest.raises((ValueError, RuntimeError), match="PROPER_NAME_MISSING"):
        run()


@pytest.mark.parametrize("provider_name", ["openai", "qwen", "qwen_mt"])
@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_provider_name_policy_parity_and_bounded_batch_repair(monkeypatch, provider_name, repair_succeeds):
    import json
    import httpx
    from document_translator.providers import QwenChatConfig, QwenChatProvider, QwenMTProvider
    from document_translator.providers.openai_api import OpenAIConfig, OpenAIProvider
    from document_translator.providers.translation_prompt import general_translation_instruction
    units = [_named_unit(text="TOTAL TENDER PRICE"), _named_unit(text="SHAREEF COLONY DS")]
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        text = "谢里夫居民区（SHAREEF COLONY）下游" if repair_succeeds and len(calls) > 1 else "谢里夫居民区下游"
        current = units if len(calls) == 1 else units[1:]
        rows = [{"id": u.id, "translation": "投标总价" if u == units[0] else text} for u in current]
        if provider_name == "openai":
            return httpx.Response(200, json={"output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps({"items": rows})}]}]})
        content = json.dumps({"items": rows}) if provider_name == "qwen" else "\n".join(
            f"[[TRB:{i:06d}]]{r['translation']}[[/TRB:{i:06d}]]" for i, r in enumerate(rows))
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    if provider_name == "openai":
        provider = OpenAIProvider(OpenAIConfig(model="gpt-5.6-luna"), client=client)
    elif provider_name == "qwen":
        provider = QwenChatProvider(QwenChatConfig(model="qwen-plus"), client=client)
    else:
        provider = QwenMTProvider(client=client)
        monkeypatch.setattr(provider, "_wait_for_request_slot", lambda: None)
    method = provider.translate_batch
    if repair_succeeds:
        assert "SHAREEF COLONY" in method(units)[1].translation
    else:
        result = method(units)
        assert result[1].validation_status == "needs_review"
        assert result[1].error is not None and "PROPER_NAME_MISSING" in result[1].error
    assert len(calls) == 2
    shared = general_translation_instruction("en", "zh-CN")
    if provider_name == "qwen_mt":
        assert shared in calls[0]["translation_options"]["domains"]
        assert {"source": "SHAREEF COLONY", "target": "SHAREEF COLONY"} in calls[0]["translation_options"]["terms"]
    else:
        prompt = calls[0]["input"] if provider_name == "openai" else calls[0]["messages"][0]["content"]
        assert shared in prompt
        data = calls[0]["input"] if provider_name == "openai" else calls[0]["messages"][1]["content"]
        assert '"required_names": ["SHAREEF COLONY"]' in data or '"required_names":["SHAREEF COLONY"]' in data
    assert "roads, bridges, and local place names" in shared
    assert "not colonial territory" in shared
