from pathlib import Path
import re

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
    assert len(units) == 1
    assert units[0].source_text == "说明 ⟦MD_0001⟧ 内容"
    translated = "Explain ⟦MD_0001⟧ content"
    assert rewrite_markdown(text, units, {units[0].id: translated}).text == "Explain `inline` content\n~~~python\nprint('keep')\n~~~\n"


def test_link_text_and_image_alt_can_change_without_targets() -> None:
    text = "查看 [文档](docs/a.md) 和 ![示意图](images/a.png)\n"
    units = extract_translation_units(text)
    assert len(units) == 1
    assert units[0].source_text == "查看 ⟦MD_0001⟧文档⟦MD_0002⟧ 和 ⟦MD_0003⟧示意图⟦MD_0004⟧"
    translated = "See ⟦MD_0001⟧document⟦MD_0002⟧ and ⟦MD_0003⟧diagram⟦MD_0004⟧"
    out = rewrite_markdown(text, units, {units[0].id: translated})
    assert out.text == "See [document](docs/a.md) and ![diagram](images/a.png)\n"


def test_front_matter_html_and_placeholder_are_protected() -> None:
    text = "---\ntitle: 不翻译键名\nauthor: someone\n---\n<b>重要</b> ⟦PH_1⟧\n"
    units = extract_translation_units(text)
    assert len(units) == 1
    assert units[0].source_text == "⟦MD_0001⟧重要⟦MD_0002⟧ ⟦PH_1⟧"
    assert units[0].protected_tokens == ["⟦MD_0001⟧", "⟦MD_0002⟧", "⟦PH_1⟧"]
    out = rewrite_markdown(text, units, {units[0].id: "⟦MD_0001⟧Important⟦MD_0002⟧ ⟦PH_1⟧"})
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


def test_empty_translation_result_keeps_source_and_reports_error() -> None:
    text = "淇濈暀\n"
    unit = extract_translation_units(text)[0]
    out = rewrite_markdown(text, [unit], {unit.id: result(unit, "   ")})
    assert out.text == text
    assert out.errors == (f"{unit.id}: MISSING_TRANSLATION",)


def test_wrong_id_and_placeholder_failure_reject_writeback() -> None:
    text = "保留 ⟦PH_1⟧\n"
    unit = extract_translation_units(text)[0]
    wrong = result(unit, "错误") .model_copy(update={"unit_id": "b" * 64})
    out = rewrite_markdown(text, [unit], {unit.id: wrong})
    assert out.text == text and any("UNIT_ID_MISMATCH" in e for e in out.errors)
    bad = result(unit, "错误 ⟦ph_1⟧")
    out = rewrite_markdown(text, [unit], {unit.id: bad})
    assert out.text == text and any("PLACEHOLDER_MISMATCH" in e for e in out.errors)


def test_generated_placeholder_tampering_rejects_whole_sentence() -> None:
    text = "请运行 `command` 后查看 https://example.test。\n"
    unit = extract_translation_units(text)[0]
    assert unit.source_text == "请运行 ⟦MD_0001⟧ 后查看 ⟦MD_0002⟧"
    candidates = [
        "Run after viewing ⟦MD_0002⟧",
        "Run ⟦MD_0001⟧ ⟦MD_0001⟧ and view ⟦MD_0002⟧",
        "Run ⟦md_0001⟧ and view ⟦MD_0002⟧",
    ]
    for candidate in candidates:
        out = rewrite_markdown(text, [unit], {unit.id: candidate})
        assert out.text == text
        assert any("PLACEHOLDER_MISMATCH" in error for error in out.errors)


def test_trailing_markdown_hard_break_spaces_stay_outside_unit() -> None:
    text = "**重要内容**  \n"
    unit = extract_translation_units(text)[0]
    assert not unit.source_text.endswith(" ")
    translated = unit.source_text.replace("重要内容", "Important")
    out = rewrite_markdown(text, [unit], {unit.id: translated})
    assert out.text == "**Important**  \n"


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
    assert units[0].protected_tokens == ["⟦MD_0001⟧"]
    translated = units[0].source_text.replace("保留的星号", "escaped star")
    out = rewrite_markdown(text, units, {units[0].id: translated})
    assert out.text == "\\* escaped star\n"


def test_inline_format_markers_and_nested_fragments_are_not_source_text() -> None:
    text = "这是 **粗体和 *嵌套斜体***，还有 _斜体_、***粗斜体***、~~删除线~~ 和 __第二段__。\n"
    units = extract_translation_units(text)
    assert len(units) == 1
    unit = units[0]
    visible_text = re.sub(r"⟦MD_\d{4}⟧", "", unit.source_text)
    assert "*" not in visible_text and "_" not in visible_text and "~" not in visible_text
    assert len(unit.protected_tokens) == 11
    translated = (unit.source_text.replace("这是", "This is").replace("粗体和", "bold and")
                  .replace("嵌套斜体", "nested italic").replace("还有", "plus")
                  .replace("粗斜体", "bold italic").replace("斜体", "italic")
                  .replace("删除线", "deleted").replace("第二段", "second"))
    out = rewrite_markdown(text, units, {unit.id: translated})
    assert "**bold and *nested italic***" in out.text
    assert "_italic_" in out.text and "***bold italic***" in out.text
    assert "~~deleted~~" in out.text and "__second__" in out.text


def test_table_cells_are_independent_and_structure_is_preserved() -> None:
    text = (
        "| 表头 | 说明 | 空列 |\n"
        "| :--- | ---: | :---: |\n"
        "| 重复 | 含 `a|b` 代码 | |\n"
        "| 重复 | [链接](https://x.test/a|b) 和转义 \\| 竖线 | 末项 |\n"
    )
    units = extract_translation_units(text)
    assert len(units) == 8
    assert [u.source_text for u in units[:3]] == ["表头", "说明", "空列"]
    assert all("|" not in u.source_text for u in units)
    assert not any("---" in u.source_text or ":---" in u.source_text for u in units)
    repeated = [u for u in units if u.source_text == "重复"]
    assert len(repeated) == 2 and repeated[0].id != repeated[1].id
    code_cell = next(u for u in units if "代码" in u.source_text)
    link_cell = next(u for u in units if "链接" in u.source_text)
    assert code_cell.source_text == "含 ⟦MD_0001⟧ 代码"
    assert "https://" not in link_cell.source_text and "\\|" not in link_cell.source_text
    translations = {u.id: u.source_text for u in units}
    translations[repeated[0].id] = "第一"
    translations[repeated[1].id] = "第二"
    out = rewrite_markdown(text, units, translations)
    assert out.errors == ()
    assert "| :--- | ---: | :---: |" in out.text
    assert "`a|b`" in out.text and "https://x.test/a|b" in out.text and "\\|" in out.text
    assert "| 第一 |" in out.text and "| 第二 |" in out.text


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
