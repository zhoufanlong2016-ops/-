from __future__ import annotations

from types import SimpleNamespace

import fitz

from document_translator.core import TranslationResult, TranslationUnit, DocumentFormat, DocumentLocation, generate_unit_id, sha256_text
from document_translator.core.validation import validate_result_for_unit
from document_translator.services import pdf_inplace
from document_translator.services.pdf_inplace import InPlacePdfTranslationService
from document_translator.translation_rules import localize_chinese_dates, rule_protected_tokens

LEFT, RIGHT, SIZE = 76.0, 524.0, 16.0


def _write_line(page: fitz.Page, x: float, y: float, chars: int, label: str = "") -> None:
    page.insert_text((x, y), (label + "甲乙丙丁戊己庚辛壬癸" * 4)[:chars], fontname="china-s", fontsize=SIZE)


def _official_page(path) -> None:
    doc = fitz.open()
    page = doc.new_page(width=600, height=850)
    # centred two-line title
    page.insert_text((180, 80), "关于修订管理办法的通知", fontname="china-s", fontsize=22)
    page.insert_text((202, 110), "管理办法实施细则", fontname="china-s", fontsize=22)
    # article 1: indented label line, a full continuation line, a short last line
    full = int((RIGHT - LEFT) // SIZE)
    _write_line(page, LEFT + 2 * SIZE, 160, full - 2, "第一条")
    _write_line(page, LEFT, 189, full)
    _write_line(page, LEFT, 218, 6)
    # article 2 starts straight after, with the same line pitch
    _write_line(page, LEFT + 2 * SIZE, 247, full - 2, "第二条")
    _write_line(page, LEFT, 276, 8)
    # non-text page objects that must survive untouched
    page.draw_line((LEFT, 300), (RIGHT, 300), color=(1, 0, 0), width=1.5)
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 20, 20), False)
    pixmap.set_rect(pixmap.irect, (200, 0, 0))
    page.insert_image(fitz.Rect(400, 140, 440, 180), pixmap=pixmap)
    doc.save(path)


def test_segments_follow_official_document_layout(tmp_path):
    source = tmp_path / "source.pdf"
    _official_page(source)
    doc = fitz.open(source)
    bounds = pdf_inplace._content_bounds(doc, [])[pdf_inplace._page_key(doc[0])]
    lines, rotated = pdf_inplace._visual_lines(doc[0], [])
    paragraphs = pdf_inplace._segment(1, lines, bounds)

    assert rotated == 0
    assert [len(p.lines) for p in paragraphs] == [2, 3, 2]
    assert paragraphs[0].centred
    assert paragraphs[1].text.startswith("第一条")
    assert paragraphs[2].text.startswith("第二条")


def test_underlined_centred_header_lines_stay_separate(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=842, height=595)
    # a page-wide body line sets the content bounds
    body = "x" * 130
    page.insert_text((60, 300), body, fontsize=11)
    centre = 60 + fitz.get_text_length(body, fontsize=11) / 2
    first, second = "LAHORE WATER PROJECT (LWWMP)", "SEWERAGE SYSTEM FROM LARECH COLONY"
    for y, text, trailing in ((60, first, "    "), (76, second, "")):
        x = centre - fitz.get_text_length(text, fontsize=11) / 2
        page.insert_text((x, y), text, fontsize=11)
        if trailing:  # a trailing blank span must not widen the line
            page.insert_text((x + fitz.get_text_length(text, fontsize=11), y), trailing, fontsize=11)
        page.draw_line((x, y + 2), (x + fitz.get_text_length(text, fontsize=11), y + 2))
    path = tmp_path / "header.pdf"
    doc.save(path)
    doc = fitz.open(path)
    bounds = pdf_inplace._content_bounds(doc, [])[pdf_inplace._page_key(doc[0])]
    lines, _ = pdf_inplace._visual_lines(doc[0], [])
    paragraphs = pdf_inplace._segment(1, lines, bounds, pdf_inplace._horizontal_rules(doc[0]))

    assert [p.text for p in paragraphs[:2]] == [first, second]
    assert all(p.centred for p in paragraphs[:2])


def test_cjk_first_line_indent_avoids_missing_nbsp_glyph():
    page = fitz.open().new_page(width=600, height=850)
    paragraph = pdf_inplace._Paragraph(1, [
        pdf_inplace._VisualLine([pdf_inplace._Segment("x", (LEFT + 2 * SIZE, 100, RIGHT, 116), [{"font": "SimSun", "size": SIZE, "color": 0, "bbox": (LEFT + 2 * SIZE, 100, RIGHT, 116), "text": "x"}])]),
        pdf_inplace._VisualLine([pdf_inplace._Segment("y", (LEFT, 120, RIGHT, 136), [{"font": "SimSun", "size": SIZE, "color": 0, "bbox": (LEFT, 120, RIGHT, 136), "text": "y"}])]),
    ], False)
    paragraph.page_width = 600.0
    content, _, _, _ = pdf_inplace._layout(page, paragraph, "中文译文", SIZE, (LEFT, RIGHT))
    assert content.startswith("　") and " " not in content


class _Provider:
    provider_name = "fake"
    config = SimpleNamespace(model="fake-model")
    prompt_version = "test"
    glossary_version = "none"

    def translate_batch(self, units):
        text = "Translated paragraph text for testing."
        return [
            TranslationResult(
                unit_id=unit.id,
                translation=text,
                provider="fake",
                model="fake-model",
                prompt_version="test",
                glossary_version="none",
                source_hash=sha256_text(unit.source_text),
                result_hash=sha256_text(text),
                request_count=1,
                validation_status="valid",
            )
            for unit in units
        ]


def test_inplace_replaces_only_text_and_keeps_page_objects(tmp_path):
    source, destination, report = tmp_path / "source.pdf", tmp_path / "out.pdf", tmp_path / "report.json"
    _official_page(source)

    output, _, _ = InPlacePdfTranslationService(_Provider()).translate_file(
        source, destination, source_language="zh", target_language="en",
        report_path=report, allow_complex_pdf=True, allow_cad_pdf=True,
    )

    before, after = fitz.open(source)[0], fitz.open(output)[0]
    text = after.get_text()
    assert not any("一" <= ch <= "鿿" for ch in text)
    assert "Translated" in text
    assert len(after.get_images(full=True)) == len(before.get_images(full=True)) == 1
    red_lines = [d for d in after.get_drawings() if d.get("color") == (1.0, 0.0, 0.0)]
    assert len(red_lines) == 1


def test_chinese_dates_become_fixed_english_and_are_protected():
    text, dates = localize_chinese_dates("于2024 年12 月5 日发布，2025年3月实施，12月31日前完成", "zh", "en")
    assert dates == ["December 5, 2024", "March 2025", "December 31"]
    assert "年" not in text and "月" not in text
    # the year inside an inserted date is not protected a second time on its own
    assert rule_protected_tokens(text, dates)[:3] == dates
    assert "2024" not in rule_protected_tokens(text, dates)
    assert localize_chinese_dates("2024年13月45日", "zh", "en") == ("2024年13月45日", [])
    assert localize_chinese_dates("2024年12月5日", "en", "zh") == ("2024年12月5日", [])


def test_word_for_word_date_translation_is_rejected():
    data = {
        "document_hash": sha256_text("doc"),
        "format": DocumentFormat.PDF,
        "location": DocumentLocation(part="page:1", object_id="x"),
        "source_language": "zh",
        "target_language": "en",
        "source_text": "日期",
        "protected_tokens": [],
        "style_signature": "paragraph",
        "context_before": "",
        "context_after": "",
    }
    unit = TranslationUnit(id=generate_unit_id(**data), **data)
    bad = "12 Month, Day 5, 2024"
    result = TranslationResult(
        unit_id=unit.id, translation=bad, provider="fake", model="m", prompt_version="t", glossary_version="n",
        source_hash=sha256_text(unit.source_text), result_hash=sha256_text(bad), request_count=1, validation_status="valid",
    )
    assert any(error.startswith("LITERAL_DATE") for error in validate_result_for_unit(unit, result))


def test_best_effort_restore_never_leaks_markers():
    from document_translator.translation_rules import protect_for_translation, restore_after_translation, restore_markers_best_effort
    import pytest

    protected = protect_for_translation("2) The EPC list of 23 stations", ["2)", "23"])
    broken = protected.text.replace(protected.replacements[1][0], "")  # provider dropped one marker
    with pytest.raises(ValueError):
        restore_after_translation(broken, protected)
    assert "[[TRP_" not in restore_markers_best_effort(broken, protected)


def test_region_does_not_reach_into_table_above():
    page = fitz.open().new_page(width=842, height=595)
    line = pdf_inplace._VisualLine([pdf_inplace._Segment("Page 2 of 10", (758, 561, 824, 576), [{"font": "ArialMT", "size": 11.0, "color": 0, "bbox": (758, 561, 824, 576), "text": "Page 2 of 10"}])])
    paragraph = pdf_inplace._Paragraph(3, [line], False)
    paragraph.page_width = 842.0
    region = pdf_inplace._region(page, paragraph, [fitz.Rect(53, 50, 795, 561)], (53, 824))
    assert region.y0 >= 561


def test_drawing_labels_stay_separate_and_frame_is_not_a_table(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=1000, height=700)
    # map frame plus a title-block row: one huge "cell" covering the drawing
    page.draw_rect(fitz.Rect(20, 30, 980, 640))
    page.draw_rect(fitz.Rect(20, 640, 980, 690))
    page.draw_line((500, 640), (500, 690))
    # labels scattered on one baseline, far apart
    for x, text in ((100, "RIVER RAVI"), (500, "RING ROAD"), (850, "DUMPING SITE")):
        page.insert_text((x, 200), text, fontsize=10)
    # a slightly tilted callout (0.6 degrees)
    origin = fitz.Point(300, 400)
    page.insert_text(origin, "AREA= 86 ACRE", fontsize=10, morph=(origin, fitz.Matrix(-0.6)))
    # legend column, one entry reaching the right edge of the content
    for y, text in ((500, "LINE B"), (520, "LATERAL BY LAHORE CANTONMENT AREA"), (540, "LINE C")):
        page.insert_text((780, y), text, fontsize=10)
    path = tmp_path / "map.pdf"
    doc.save(path)
    doc = fitz.open(path)
    lines, rotated = pdf_inplace._visual_lines(doc[0], [])
    paragraphs = pdf_inplace._segment(1, lines, (100, 970))

    texts = [p.text for p in paragraphs]
    assert rotated == 0
    assert {"RIVER RAVI", "RING ROAD", "DUMPING SITE", "AREA= 86 ACRE"} <= set(texts)
    assert all(len(p.lines) == 1 for p in paragraphs)

    from document_translator.services import pdf_table
    tables = pdf_table.extract_pdf_tables(path)
    assert all(pdf_inplace._is_drawing_frame(t, doc[0]) for t in tables if t.page_number == 1)


def test_cited_document_numbers_keep_brackets():
    source = "集团公司《实施细则（试行）》（中土经营〔2024〕341号）相关要求"
    fix = pdf_inplace._bracket_cited_references
    assert fix(source, "(Zhongtu Jingying 2024 No. 341), the") == "(Zhongtu Jingying [2024] No. 341), the"
    assert fix(source, "(CCECC Operations [2024]341 No.), to") == "(CCECC Operations [2024] No. 341), to"
    assert fix(source, "(Zhongtu 2024 No. 3410)") == "(Zhongtu 2024 No. 3410)"


def test_shared_probe_matches_a_fresh_probe_document():
    from document_translator.services.pdf_table import probe_textbox

    fontfile = r"C:\Windows\Fonts\simhei.ttf"
    text = "请提供每个泵站中压水泵的单线图以及所有相关接口细节。" * 3
    for size in (12.0, 10.0, 8.0, 6.0):
        for width, height in ((300, 120), (150, 60)):
            doc = fitz.open()
            page = doc.new_page(width=width + 20, height=height + 20)
            fresh = page.insert_textbox(fitz.Rect(0, 0, width, height), text, fontname="probe", fontfile=fontfile, fontsize=size)
            lines = sum(len(b.get("lines", [])) for b in page.get_text("dict")["blocks"] if b.get("type") == 0)
            doc.close()
            assert probe_textbox(width, height, text, fontfile=fontfile, fontname="probe", fontsize=size, count_lines=True) == (fresh, lines)


def _unit(text: str, index: int) -> TranslationUnit:
    data = {
        "document_hash": sha256_text("doc"),
        "format": DocumentFormat.PDF,
        "location": DocumentLocation(part=f"page:{index}", object_id=f"p{index}"),
        "source_language": "en",
        "target_language": "zh",
        "source_text": text,
        "protected_tokens": [],
        "style_signature": "paragraph",
        "context_before": "",
        "context_after": "",
    }
    return TranslationUnit(id=generate_unit_id(**data), **data)


class _CountingProvider:
    provider_name = "fake"
    config = SimpleNamespace(model="fake-model", batch_input_characters=200)
    prompt_version = "test"
    glossary_version = "none"

    def __init__(self):
        import threading
        self.lock = threading.Lock()
        self.sent: list[str] = []
        self.active = 0
        self.max_active = 0

    def translate_batch(self, units):
        import time
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.sent.extend(u.source_text for u in units)
        time.sleep(0.05)
        with self.lock:
            self.active -= 1
        return [
            TranslationResult(
                unit_id=u.id, translation="这是一段译文内容", provider="fake", model="fake-model",
                prompt_version="test", glossary_version="none", source_hash=sha256_text(u.source_text),
                result_hash=sha256_text("这是一段译文内容"), request_count=1, validation_status="valid",
            )
            for u in units
        ]


def test_translate_all_dedups_runs_concurrently_and_reuses_cache(tmp_path):
    from document_translator.services.cache import TranslationCache

    texts = [f"Distinct paragraph number {i:03d} with some words" for i in range(40)]
    units = [_unit(t, i) for i, t in enumerate(texts)] + [_unit("Running header text", 100 + i) for i in range(30)]
    provider = _CountingProvider()
    with TranslationCache(tmp_path / "cache.sqlite3") as cache:
        translations, warnings, stats = pdf_inplace._translate_all(provider, units, cache=cache)
        assert set(translations) == {u.id for u in units}
        assert provider.sent.count("Running header text") == 1
        assert stats["distinct_texts"] == 41 and stats["requests"] > 1
        assert provider.max_active > 1
        rerun = _CountingProvider()
        again, _, stats2 = pdf_inplace._translate_all(rerun, units, cache=cache)
        assert rerun.sent == [] and stats2["from_cache"] == 41
        assert again == translations


def test_line_broken_after_hyphen_joins_without_space():
    line = lambda text: pdf_inplace._VisualLine([pdf_inplace._Segment(text, (0, 0, 10, 10), [{"size": 10.0}])])
    paragraph = pdf_inplace._Paragraph(1, [line("Drawing No. LW-"), line("TD-401 through")], False)
    assert paragraph.text == "Drawing No. LW-TD-401 through"


def test_bare_codes_are_not_sent_for_translation():
    assert not pdf_inplace._needs_translation("J01-L3C", "en")
    assert not pdf_inplace._needs_translation("R05-A", "en")
    assert pdf_inplace._needs_translation("AREA= 86 ACRE", "en")


def test_cjk_paragraph_never_breaks_inside_a_code(tmp_path):
    source = tmp_path / "source.pdf"
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((20, 40), "a clean gas-based fire suppression system e.g. Inergen (IG-541) or FM200", fontsize=6)
    doc.save(source)
    lines, _ = pdf_inplace._visual_lines(fitz.open(source)[0], [])
    paragraph = pdf_inplace._Paragraph(1, lines, False, page_width=300, bounds=(20.0, 280.0))
    text = "承包商应设计并提供洁净气体灭火系统，例如 Inergen (IG-541) 或 FM200。" * 3
    for width in range(90, 200, 3):
        content, *_ = pdf_inplace._layout(fitz.open(source)[0], paragraph, text, 10.0, (20.0, 280.0), float(width))
        assert all("IG-" not in row or "IG-541" in row for row in content.split("\n"))


def test_map_marker_glyphs_are_neither_text_nor_a_merge_bridge(tmp_path):
    source = tmp_path / "map.pdf"
    doc = fitz.open()
    page = doc.new_page(width=300, height=200)
    page.insert_text((20, 40), "J01-L3C", fontsize=5)
    page.insert_text((43, 40), "!(", fontsize=5, fontname="Symbol")
    page.insert_text((52, 40), "BEIGUM Rd.", fontsize=5)
    doc.save(source)
    lines, _ = pdf_inplace._visual_lines(fitz.open(source)[0], [])
    assert sorted(line.text for line in lines) == ["BEIGUM Rd.", "J01-L3C"]


def test_code_digits_are_not_an_inline_list_marker():
    from document_translator.services.pdf_table import _break_inline_list_markers

    assert "\n" not in _break_inline_list_markers("例如 Inergen (IG-541) 或 FM200。")
    assert _break_inline_list_markers("要求如下 1. 第一项 2. 第二项").count("\n") == 2


def test_acronyms_in_a_dated_cell_are_not_untranslated_english():
    from document_translator.translation_rules import validate_translation_residue

    errors = validate_translation_residue(
        "PMC shall approve the windows by 12th March 2025", "PMC 应于2025年3月12日批准 windows", "en", "zh"
    )
    assert not any("'PMC'" in error for error in errors)
    assert any("'windows'" in error for error in errors)


def test_bracketed_quantity_is_not_an_inline_list_marker():
    from document_translator.services.pdf_table import _break_inline_list_markers

    assert "\n" not in _break_inline_list_markers("投标人需提供六 (06) 台新制造的设备")


def test_chinese_wrap_keeps_punctuation_off_line_starts_and_brackets_off_line_ends():
    from document_translator.services.pdf_table import _wrap_atomic_phrases

    text = "业主认为，已经为投标人提供了三（03）个月的准备投标时间，这被认为是充分的。因此，遗憾地拒绝进一步延长投标提交日期。"
    for width in range(120, 300, 7):
        lines = _wrap_atomic_phrases(text, fontfile=r"C:\Windows\Fonts\simhei.ttf", fontname="x", fontsize=10, max_width=width).split("\n")
        assert not any(line[:1] in "，。、；：）" for line in lines[1:])
        assert not any(line.endswith("（") for line in lines)


def test_row_continued_on_next_page_is_one_text_split_back_at_punctuation():
    head, tail = pdf_inplace._split_continued("投标人将采购、制造并部署所需数量的设备，完全符合规定。", 0.3)
    assert head.endswith(("、", "，")) and head + tail == "投标人将采购、制造并部署所需数量的设备，完全符合规定。"


def test_multi_word_glossary_terms_match_in_any_case():
    from document_translator.services.glossary import load_glossary

    glossary = load_glossary(r"D:\01 绿色程序\文档翻译器\src\document_translator\assets\engineering_en_zh_glossary.csv")
    assert [e.target for e in glossary.entries_for("the Ultimate disposal station")] == ["最终处置站"]
    assert glossary.entries_for("the answer is no.") == ()


def test_page_footers_have_one_fixed_form():
    from types import SimpleNamespace

    assert pdf_inplace._page_footer_translation(SimpleNamespace(source_text="Page 2 of 2", target_language="zh")) == "第2页，共2页"
    assert pdf_inplace._page_footer_translation(SimpleNamespace(source_text="第 1 页，共 2 页", target_language="en")) == "Page 1 of 2"
    assert pdf_inplace._page_footer_translation(SimpleNamespace(source_text="Page 2 of the report", target_language="zh")) is None


def test_right_aligned_running_header_stays_one_paragraph():
    line = lambda x0, y, text: pdf_inplace._VisualLine([pdf_inplace._Segment(text, (x0, y, 785.0, y + 11), [{"size": 10.0, "color": 0}])])
    lines = [line(379.0, 38.0, "Lahore Wastewater Project (LWDMP) - Sewerage System from"), line(449.0, 51.0, "Larechs Colony to Gulshan E Ravi, Lahore")]
    assert len(pdf_inplace._segment(1, lines, (72.0, 785.0))) == 1


def test_numbered_item_with_hanging_indent_stays_one_paragraph():
    line = lambda x0, y, text: pdf_inplace._VisualLine([pdf_inplace._Segment(text, (x0, y, 523.0, y + 13), [{"size": 11.0, "color": 0}])])
    lines = [line(72.0, 402.0, "1. In partial modification to the above referred SPN"), line(90.0, 416.0, "Section 2 - (Tender Data Sheet) Page 2-9"), line(90.0, 430.0, "Documents; the Tender submission deadline")]
    assert len(pdf_inplace._segment(1, lines, (72.0, 526.0))) == 1


def test_numbered_sub_items_go_back_on_their_own_lines():
    source = "2.1.1. Inception Report Approved\n2.1.2 Liaison with stakeholders for NOC\n2.1.3 Design Team mobilized"
    restored = pdf_inplace._restore_item_breaks(source, "2.1.1.启动报告获批2.1.2.与利益相关者联络 2.1.3设计团队进场")
    assert restored.split("\n") == ["2.1.1.启动报告获批", "2.1.2.与利益相关者联络", "2.1.3设计团队进场"]
    assert pdf_inplace._restore_item_breaks("Refer to Sub-Clause 2.1.1", "参见第2.1.1款") == "参见第2.1.1款"


def test_full_width_rows_are_not_folded_into_the_cell_above():
    from document_translator.services.pdf_table import PdfTableCell, _merge_phantom_rows

    cell = lambda row, column, rect, text="x": PdfTableCell(id=f"r{row}c{column}", page_number=1, table_number=1, row=row, column=column, text=text, rect=rect, source_font_size=10.0)
    cells = (
        cell(1, 1, (82, 162, 287, 232)), cell(1, 2, (287, 162, 514, 232)),
        cell(2, 1, (82, 232, 514, 301), "Note: ..."), cell(2, 2, None, ""),
        cell(3, 1, (82, 301, 514, 338), "Schedule No. 4"), cell(3, 2, None, ""),
    )
    folded = {c.id: c for c in _merge_phantom_rows(cells, 3, 2)}
    assert folded["r2c1"].text == "Note: ..." and folded["r3c1"].text == "Schedule No. 4"
