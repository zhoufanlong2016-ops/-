import json

import pytest
from pydantic import ValidationError

from document_translator.core import (
    DocumentFormat, DocumentLocation, TranslationResult, TranslationUnit, UnitStatus,
    generate_cache_key, generate_unit_id, sha256_text, validate_glossary_terms, validate_placeholders,
    validate_result_for_unit,
)


def make_unit(**changes) -> TranslationUnit:
    data = dict(
        document_hash="a" * 64, format=DocumentFormat.MD,
        location=DocumentLocation(part="README.md", object_id="p1", node_ids=["n1"]),
        source_language="zh-CN", target_language="en", source_text="保留 ⟦PH_001⟧。",
        protected_tokens=["⟦PH_001⟧"], style_signature="style-v1",
        context_before="前文", context_after="后文", status=UnitStatus.PENDING,
    )
    data.update(changes)
    if "id" not in changes:
        data["id"] = generate_unit_id(**{k: v for k, v in data.items() if k != "status"})
    return TranslationUnit.model_validate(data)


def make_result(unit: TranslationUnit, translation="Keep ⟦PH_001⟧.") -> TranslationResult:
    return TranslationResult(
        unit_id=unit.id, translation=translation, provider="test", model="deterministic",
        prompt_version="p1", glossary_version="g1", source_hash=sha256_text(unit.source_text),
        result_hash=sha256_text(translation), request_count=1, validation_status="valid",
    )


def test_models_json_round_trip() -> None:
    unit = make_unit()
    assert TranslationUnit.model_validate_json(unit.model_dump_json()) == unit
    result = make_result(unit)
    assert TranslationResult.model_validate_json(result.model_dump_json()) == result
    assert json.loads(unit.model_dump_json())["format"] == "md"


def test_valid_translation_and_placeholders() -> None:
    unit = make_unit()
    result = make_result(unit)
    assert validate_placeholders(unit.source_text, result.translation, unit.protected_tokens) == []
    assert validate_result_for_unit(unit, result) == []


def test_result_hash_mismatch_is_error() -> None:
    unit = make_unit()
    result = make_result(unit).model_copy(update={"result_hash": "b" * 64})
    assert "RESULT_HASH_MISMATCH" in validate_result_for_unit(unit, result)


def test_glossary_terms_require_exact_target_occurrence() -> None:
    assert validate_glossary_terms("control panel", "控制柜", [("control panel", "控制柜")]) == []
    errors = validate_glossary_terms("control panel", "控制面板", [("control panel", "控制柜")])
    assert errors and "GLOSSARY_TERM_MISSING" in errors[0]


def test_glossary_terms_accept_english_case_variants() -> None:
    assert validate_glossary_terms("污水管道", "Sewer Pipeline", [("污水管道", "sewer pipeline")]) == []


@pytest.mark.parametrize("translation", ["Keep.", "Keep ⟦ph_001⟧.", "Keep ⟦PH_001⟧ ⟦PH_001⟧."])
def test_missing_case_or_duplicate_placeholder_is_error(translation: str) -> None:
    assert validate_placeholders("⟦PH_001⟧", translation, ["⟦PH_001⟧"])


def test_placeholder_validation_does_not_modify_translation() -> None:
    translation = "Keep ⟦PH_001⟧ ⟦PH_001⟧."
    validate_placeholders("⟦PH_001⟧", translation, ["⟦PH_001⟧"])
    assert translation == "Keep ⟦PH_001⟧ ⟦PH_001⟧."


def test_wrong_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="stable unit identity"):
        make_unit(id="b" * 64)


def test_unknown_format_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_unit(format="xls")


def test_xlsx_format_is_accepted() -> None:
    assert make_unit(format="xlsx").format is DocumentFormat.XLSX


def test_empty_source_text_is_rejected() -> None:
    with pytest.raises(ValidationError):
        make_unit(source_text="")


def test_stable_hashes_and_cache_keys() -> None:
    unit = make_unit()
    assert generate_unit_id(**unit.model_dump(exclude={"id", "status"})) == unit.id
    kwargs = dict(source_text="abc", source_language="en", target_language="zh-CN",
                  model="m", prompt_version="p1", glossary_version="g1",
                  protected_tokens=["⟦X⟧"])
    assert generate_cache_key(**kwargs) == generate_cache_key(**kwargs)


def test_list_label_before_latin_word_is_counted_in_source():
    source = "1. Kindly refer to Section 6"
    assert validate_placeholders(source, "1. 请参阅第6节", ["1. "]) == []
    assert validate_placeholders(source, "请参阅第6节", ["1. "])


def test_far_too_short_translation_is_flagged_incomplete():
    from document_translator.core.validation import _incomplete_translation
    from types import SimpleNamespace

    unit = SimpleNamespace(source_language="en", target_language="zh", source_text="x" * 600)
    assert _incomplete_translation(unit, "译" * 85)
    assert _incomplete_translation(unit, "译" * 150) is None
