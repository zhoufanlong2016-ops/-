from document_translator.providers.translation_prompt import compile_translation_policy
from tests.test_qwen_mt_provider import make_unit


def test_compiled_policy_is_shared_by_prompt_and_qwen_options() -> None:
    unit = make_unit()

    policy = compile_translation_policy(unit)

    assert "Engineering and contract terminology" in policy.instruction
    assert "Preserve every number with its unit" in policy.qwen_domain


def test_policy_does_not_convert_chinese_section_ordinals_to_arabic_digits() -> None:
    policy = compile_translation_policy(make_unit())
    assert "do not introduce Arabic numerals" in policy.instruction
    assert ("[[TOKEN_1]]", "[[TOKEN_1]]") in policy.required_terms


def test_policy_filters_reverse_direction_glossary_terms() -> None:
    from document_translator.core import DocumentFormat, DocumentLocation, TranslationUnit, generate_unit_id, sha256_text
    from document_translator.services import Glossary, GlossaryEntry
    glossary = Glossary(entries=(
        GlossaryEntry(source="control point", target="控制点"),
        GlossaryEntry(source="控制点", target="control point"),
    ), version="v1")
    data = dict(document_hash=sha256_text("x"), format=DocumentFormat.XLSX,
                location=DocumentLocation(part="sheet", object_id="A1"), source_language="zh-CN",
                target_language="en", source_text="控制点", protected_tokens=[])
    policy = compile_translation_policy(TranslationUnit(id=generate_unit_id(**data), **data), glossary)
    assert ("控制点", "control point") in policy.required_terms
    assert ("control point", "控制点") not in policy.required_terms
