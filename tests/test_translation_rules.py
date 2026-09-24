import pytest

from document_translator.translation_rules import (
    protect_for_translation,
    restore_after_translation,
    rule_protected_tokens,
)
from document_translator.core.validation import validate_placeholders


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
