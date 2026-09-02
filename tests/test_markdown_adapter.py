from pathlib import Path

from document_translator.adapters.markdown import (
    extract_translation_units, read_markdown, rewrite_markdown, write_markdown,
)
from document_translator.core import TranslationResult, sha256_text


def result(unit, translation):
    return TranslationResult(
        unit_id=unit.id, translation=translation, provider="test", model="none",
        prompt_version="p1", glossary_version="g1", source_hash=sha256_text(unit.source_text),
        result_hash=sha256_text(translation), request_count=1, validation_status="valid",
    )


def test_extracts_paragraphs_and_multilevel_headings() -> None:
    units = extract_translation_units("# 一级\n## 二级\n普通段落\n")
    assert [u.source_text for u in units] == ["一级", "二级", "普通段落"]
    assert [u.location.object_id for u in units] == ["line:1:span:2:4", "line:2:span:3:5", "line:3:span:0:4"]


def test_lists_quote_and_nested_lists_keep_markers() -> None:
    text = "1. 有序\n- 无序\n  - 嵌套\n> 引用\n"
    units = extract_translation_units(text)
    assert [u.source_text for u in units] == ["有序", "无序", "嵌套", "引用"]
    rewritten = rewrite_markdown(text, units, {u.id: f"T-{u.source_text}" for u in units})
    assert rewritten.errors == ()
    assert rewritten.text == "1. T-有序\n- T-无序\n  - T-嵌套\n> T-引用\n"


def test_inline_and_fenced_code_are_unchanged() -> None:
    text = "说明 `inline` 内容\n~~~python\nprint('keep')\n~~~\n"
    units = extract_translation_units(text)
    assert [u.source_text for u in units] == ["说明", "内容"]
    assert rewrite_markdown(text, units, {u.id: "译文" for u in units}).text == "译文 `inline` 译文\n~~~python\nprint('keep')\n~~~\n"


def test_link_text_and_image_alt_can_change_without_targets() -> None:
    text = "查看 [文档](docs/a.md) 和 ![示意图](images/a.png)\n"
    units = extract_translation_units(text)
    assert [u.source_text for u in units] == ["查看", "文档", "和", "示意图"]
    out = rewrite_markdown(text, units, {u.id: "X" for u in units})
    assert out.text == "X [X](docs/a.md) X ![X](images/a.png)\n"


def test_front_matter_html_and_placeholder_are_protected() -> None:
    text = "---\ntitle: 不翻译键名\nauthor: someone\n---\n<b>重要</b> ⟦PH_1⟧\n"
    units = extract_translation_units(text)
    assert len(units) == 1
    assert units[0].source_text == "<b>重要</b> ⟦PH_1⟧"
    assert units[0].protected_tokens == ["<b>", "</b>", "⟦PH_1⟧"]
    out = rewrite_markdown(text, units, {units[0].id: "<b>Important</b> ⟦PH_1⟧"})
    assert out.text == "---\ntitle: 不翻译键名\nauthor: someone\n---\n<b>Important</b> ⟦PH_1⟧\n"


def test_repeated_source_text_uses_separate_spans() -> None:
    text = "重复\n重复\n"
    units = extract_translation_units(text)
    out = rewrite_markdown(text, units, {units[0].id: "第一", units[1].id: "第二"})
    assert out.text == "第一\n第二\n"
    assert units[0].id != units[1].id


def test_missing_translation_keeps_source_and_reports_error() -> None:
    text = "保留\n"
    unit = extract_translation_units(text)[0]
    out = rewrite_markdown(text, [unit], {})
    assert out.text == text
    assert "MISSING_TRANSLATION" in out.errors[0]


def test_empty_translation_keeps_source_and_reports_error() -> None:
    text = "保留\n"
    unit = extract_translation_units(text)[0]
    out = rewrite_markdown(text, [unit], {unit.id: "   "})
    assert out.text == text and "MISSING_TRANSLATION" in out.errors[0]


def test_wrong_id_and_placeholder_failure_reject_writeback() -> None:
    text = "保留 ⟦PH_1⟧\n"
    unit = extract_translation_units(text)[0]
    wrong = result(unit, "错误") .model_copy(update={"unit_id": "b" * 64})
    out = rewrite_markdown(text, [unit], {unit.id: wrong})
    assert out.text == text and any("UNIT_ID_MISMATCH" in e for e in out.errors)
    bad = result(unit, "错误 ⟦ph_1⟧")
    out = rewrite_markdown(text, [unit], {unit.id: bad})
    assert out.text == text and any("PLACEHOLDER_MISMATCH" in e for e in out.errors)


def test_line_endings_and_final_newline_are_preserved() -> None:
    for text in ("第一\r\n第二\r\n", "第一\r\n第二", "第一\n第二\n", "第一\n第二"):
        units = extract_translation_units(text)
        out = rewrite_markdown(text, units, {u.id: "译" for u in units})
        assert out.text.endswith("\r\n") is text.endswith("\r\n")
        assert out.text.endswith("\n") is text.endswith("\n")


def test_no_translations_is_byte_for_byte_identical_and_stable() -> None:
    text = "# 中文\r\n\n路径 `x`\r\n"
    first = extract_translation_units(text)
    second = extract_translation_units(text)
    out = rewrite_markdown(text, first, {})
    assert out.text.encode("utf-8") == text.encode("utf-8")
    assert [(u.id, u.location.object_id) for u in first] == [(u.id, u.location.object_id) for u in second]


def test_escaped_markdown_character_is_protected() -> None:
    text = "\\* 保留的星号\n"
    units = extract_translation_units(text)
    assert units[0].protected_tokens == ["\\*"]
    translated = units[0].source_text.replace("保留的星号", "escaped star")
    out = rewrite_markdown(text, units, {units[0].id: translated})
    assert out.text == "\\* escaped star\n"


def test_utf8_chinese_path_read_write_and_no_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "中文 文档.md"
    destination = tmp_path / "输出.md"
    source.write_bytes("中文\n".encode("utf-8"))
    loaded = read_markdown(source)
    assert loaded.encoding == "utf-8"
    unit = extract_translation_units(loaded.text)[0]
    write_markdown(destination, rewrite_markdown(loaded.text, [unit], {unit.id: "Chinese"}).text)
    assert destination.read_bytes() == b"Chinese\n"
    try:
        write_markdown(destination, "覆盖")
    except FileExistsError:
        pass
    else:
        raise AssertionError("write_markdown must not overwrite by default")
