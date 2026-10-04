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
    folded = {c.id: c for c in _merge_phantom_rows(cells, 3, 2, dividers=(162.0, 232.0, 301.0, 338.0))}
    assert folded["r2c1"].text == "Note: ..." and folded["r3c1"].text == "Schedule No. 4"


def test_cell_text_reaches_the_model_one_item_per_line():
    structure = pdf_inplace._structure_cell_text
    assert structure("2.1.1. Inception Report Approved\n2.1.2 Liaison with stakeholders for\nNOC\n2.1.3 Design Team mobilized") == (
        "2.1.1. Inception Report Approved\n2.1.2 Liaison with stakeholders for NOC\n2.1.3 Design Team mobilized"
    )
    assert structure("prices.\n70% of proportion of each site on delivery of\ncomplete equipment") == (
        "prices.\n70% of proportion of each site on delivery of complete equipment"
    )
    assert structure("Volume 1, Part I, Section\n2, Tender Data Sheet, ITT\n24.1") == "Volume 1, Part I, Section 2, Tender Data Sheet, ITT 24.1"
    assert structure("payable within\n10 days after approval") == "payable within 10 days after approval"


def test_body_set_inside_the_page_margins_keeps_its_paragraphs(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "STORM WATER DRAINS IN CENTRAL ZONE", fontsize=12)
    body = [
        "The drainage network of Central Zone comprises mainly of Cantonment Drain",
        "and its tributaries. Total catchment area for Cantonment Drain has been",
        "estimated to be 23,653 acres. Some secondary and tertiary drains catering",
        "to specific areas are as below",
    ]
    for index, text in enumerate(body):
        page.insert_textbox(fitz.Rect(90, 110 + 17 * index, 527, 130 + 17 * index), text, fontsize=10.5, align=3 if index < 3 else 0)
    lines, _ = pdf_inplace._visual_lines(page, [])
    for line in lines[1:4]:
        line.segments[0].bbox = (90.0, line.bbox[1], 527.0, line.bbox[3])
    bounds = (36.0, 549.0)  # margins of other pages of this size
    paragraphs = pdf_inplace._segment(1, lines, pdf_inplace._body_edges(lines * 2, bounds), [])
    assert [len(p.lines) for p in paragraphs] == [1, 4]



# Border geometry of two risk-register tables from a Word-made PDF: every
# rule is a thin filled rectangle (no text from the document is kept).
_P67_RULES = [[(33.2, 110.4, 764.0, 111.4)], [(764.0, 110.4, 765.0, 111.4), (33.2, 111.4, 34.2, 140.4)], [(764.0, 111.4, 765.0, 140.4)], [(33.2, 140.4, 764.0, 141.4)], [(764.0, 140.4, 765.0, 141.4), (33.2, 141.4, 34.2, 225.5), (56.8, 141.4, 57.7, 225.5), (164.4, 141.4, 165.4, 225.5), (320.6, 141.4, 321.6, 225.5), (454.4, 141.4, 455.4, 225.5), (612.5, 141.4, 613.4, 225.5), (707.0, 141.4, 708.0, 225.5)], [(764.0, 141.4, 765.0, 225.5)], [(33.2, 225.5, 764.0, 226.4)], [(764.0, 225.5, 765.0, 226.4), (33.2, 226.4, 34.2, 245.0), (56.8, 226.4, 57.7, 245.0)], [(764.0, 226.4, 765.0, 245.0)], [(33.2, 245.0, 34.2, 246.0)], [(56.8, 245.0, 764.0, 246.0)], [(764.0, 245.0, 765.0, 246.0), (33.2, 246.0, 34.2, 319.0), (56.8, 246.0, 57.7, 319.0), (164.4, 246.0, 165.4, 319.0), (320.6, 246.0, 321.6, 319.0), (454.4, 246.0, 455.4, 319.0), (612.5, 246.0, 613.4, 319.0), (707.0, 246.0, 708.0, 319.0)], [(764.0, 246.0, 765.0, 319.0)], [(33.2, 319.0, 34.2, 319.9)], [(56.8, 319.0, 164.4, 319.9)], [(164.4, 319.0, 165.4, 319.9)], [(165.4, 319.0, 320.6, 319.4)], [(320.6, 319.0, 321.6, 319.9)], [(321.6, 319.0, 454.4, 319.4)], [(454.4, 319.0, 455.4, 319.9)], [(455.4, 319.0, 612.5, 319.4)], [(612.5, 319.0, 613.4, 319.9)], [(613.4, 319.0, 707.0, 319.4)], [(707.0, 319.0, 708.0, 319.9)], [(708.0, 319.0, 764.0, 319.4)], [(764.0, 319.0, 765.0, 319.9), (33.2, 319.9, 34.2, 410.5), (56.8, 319.9, 57.7, 410.5), (164.4, 319.9, 165.4, 410.5), (320.6, 319.9, 321.6, 410.5), (454.4, 319.9, 455.4, 410.5), (612.5, 319.9, 613.4, 410.5), (707.0, 319.9, 708.0, 410.5)], [(764.0, 319.9, 765.0, 410.5)], [(33.2, 410.5, 34.2, 411.5)], [(56.8, 410.5, 707.0, 411.5)], [(707.0, 410.5, 708.0, 411.5)], [(708.0, 410.5, 764.0, 411.0)], [(764.0, 410.5, 765.0, 411.5), (33.2, 411.5, 34.2, 504.2), (56.8, 411.5, 57.7, 504.2), (164.4, 411.5, 165.4, 504.2), (320.6, 411.5, 321.6, 504.2), (454.4, 411.5, 455.4, 504.2), (612.5, 411.5, 613.4, 504.2), (707.0, 411.5, 708.0, 504.2)], [(764.0, 411.5, 765.0, 504.2)], [(33.2, 504.2, 764.0, 505.2)], [(764.0, 504.2, 765.0, 505.2)], [(33.2, 505.2, 34.2, 527.9)], [(33.2, 526.9, 56.8, 527.9)], [(56.8, 505.2, 57.7, 527.9)], [(57.7, 526.9, 764.0, 527.9)], [(764.0, 505.2, 765.0, 527.9)], [(764.0, 526.9, 765.0, 527.9)]]
_P67_TEXT_AT = [(28.0, 68.3), (28.0, 78.6), (284.8, 100.4), (206.4, 129.7), (40.8, 177.6), (40.8, 190.9), (103.0, 177.6), (203.0, 177.6), (377.0, 177.6), (472.6, 177.6), (623.8, 164.6), (618.2, 179.5), (617.5, 194.3), (626.0, 209.2), (623.2, 223.9), (708.0, 155.3), (716.3, 168.6), (729.1, 183.4), (716.3, 198.2), (34.2, 240.9), (34.2, 255.4), (34.2, 269.8), (34.2, 284.4), (34.2, 298.9), (34.2, 313.4), (34.2, 327.8), (34.2, 342.3), (34.2, 357.3), (42.6, 371.8), (133.8, 243.7), (169.8, 243.7), (205.7, 243.7), (241.8, 243.7), (277.8, 243.7), (313.8, 243.7), (349.8, 243.7), (57.8, 259.7), (59.6, 273.0), (59.6, 287.9), (59.6, 302.6), (59.6, 317.5), (165.4, 279.1), (321.6, 259.6), (323.3, 272.8), (323.3, 287.3), (457.1, 266.0), (457.1, 280.3), (457.1, 294.8), (641.5, 279.1), (616.0, 292.4), (626.5, 305.6), (641.9, 319.0), (736.0, 269.3), (726.2, 282.5), (736.0, 295.8), (746.4, 309.0), (57.8, 346.9), (59.6, 360.4), (59.6, 375.2), (167.0, 353.9), (167.0, 368.4), (167.0, 382.8), (167.0, 397.2), (323.3, 353.9), (381.4, 353.9), (433.2, 353.9), (323.3, 368.4), (323.3, 382.8), (323.3, 397.2), (455.5, 333.4), (457.1, 346.6), (457.1, 361.0), (457.1, 375.5), (457.1, 389.9), (457.1, 404.3), (613.6, 346.6), (613.6, 360.1), (647.8, 373.4), (646.1, 386.8), (644.2, 400.0), (716.9, 371.9), (57.8, 425.2), (59.6, 438.5), (59.6, 453.4), (59.6, 468.2), (59.6, 483.0), (165.4, 438.2), (167.0, 451.6), (167.0, 466.0), (321.6, 438.2), (323.3, 451.6), (323.3, 466.0), (457.1, 445.2), (457.1, 459.7), (457.1, 474.1), (457.1, 488.5), (457.1, 503.0), (613.6, 438.1), (644.4, 458.4), (644.2, 471.7), (647.8, 485.0), (724.9, 464.5), (34.2, 519.7), (312.1, 521.2), (710.3, 600.4)]

_P71_RULES = [[(33.5, 67.2, 757.4, 68.2)], [(757.4, 67.2, 758.4, 68.2), (33.5, 68.2, 34.4, 97.2)], [(757.4, 68.2, 758.4, 97.2)], [(33.5, 97.2, 757.4, 98.2)], [(757.4, 97.2, 758.4, 98.2), (33.5, 98.2, 34.4, 167.4), (76.2, 98.2, 77.2, 167.4), (194.4, 98.2, 195.4, 167.4), (322.1, 98.2, 323.0, 167.4), (439.7, 98.2, 440.6, 167.4), (569.5, 98.2, 570.5, 167.4), (694.3, 98.2, 695.3, 167.4)], [(757.4, 98.2, 758.4, 167.4)], [(33.5, 167.4, 757.4, 168.4)], [(757.4, 167.4, 758.4, 168.4)], [(33.5, 168.4, 34.4, 278.6)], [(33.5, 277.7, 76.2, 278.6)], [(76.2, 168.4, 77.2, 278.6)], [(77.2, 277.7, 194.4, 278.6)], [(194.4, 168.4, 195.4, 278.6)], [(195.4, 277.7, 322.1, 278.6)], [(322.1, 168.4, 323.0, 278.6)], [(323.0, 277.7, 439.7, 278.6)], [(439.7, 168.4, 440.6, 278.6)], [(440.6, 277.7, 569.5, 278.6)], [(569.5, 168.4, 570.5, 278.6)], [(570.5, 277.7, 694.3, 278.6)], [(694.3, 168.4, 695.3, 278.6)], [(695.3, 277.7, 757.4, 278.6)], [(757.4, 168.4, 758.4, 278.6)], [(757.4, 277.7, 758.4, 278.6)]]
_P71_TEXT_AT = [(28.0, 67.1), (203.3, 86.5), (47.9, 134.4), (47.9, 147.7), (109.2, 134.4), (224.3, 134.4), (457.7, 134.4), (457.7, 147.7), (595.8, 121.4), (575.9, 136.3), (574.8, 151.1), (588.0, 166.0), (695.3, 112.0), (696.2, 125.3), (708.1, 140.2), (696.2, 154.9), (34.6, 195.0), (34.6, 208.4), (52.6, 222.9), (77.3, 195.4), (79.1, 208.8), (79.1, 223.6), (79.1, 238.4), (79.1, 253.2), (79.1, 268.1), (195.5, 182.2), (197.2, 195.5), (197.2, 209.9), (197.2, 224.3), (197.2, 238.8), (197.2, 253.2), (197.2, 267.6), (323.2, 189.5), (323.2, 203.8), (323.2, 218.3), (323.2, 232.7), (323.2, 247.1), (442.2, 189.8), (442.2, 204.1), (442.2, 218.6), (442.2, 233.0), (442.2, 247.4), (442.2, 262.0), (442.2, 276.4), (570.5, 195.0), (595.9, 215.5), (619.0, 229.9), (695.3, 195.0), (695.3, 208.4), (717.1, 221.8), (28.0, 291.0), (710.3, 600.4)]


def _ruled_page(tmp_path, rules, text_at):
    doc = fitz.open()
    page = doc.new_page(width=792, height=612)
    shape = page.new_shape()
    for group in rules:  # one filled path per group, as drawn in the source
        for rect in group:
            shape.draw_rect(fitz.Rect(rect))
        shape.finish(fill=(0, 0, 0), color=None, width=0)
    shape.commit()
    for x, y in text_at:
        page.insert_text((x, y - 2), "text", fontsize=8)
    path = tmp_path / "ruled.pdf"
    doc.save(path)
    return path


def test_column_rules_drawn_as_filled_rectangles_are_columns(tmp_path):
    from document_translator.services import pdf_table

    tables = pdf_table.extract_pdf_tables(_ruled_page(tmp_path, _P67_RULES, _P67_TEXT_AT))
    assert [(t.row_count, t.column_count) for t in tables] == [(7, 7)]


def test_a_header_row_is_not_read_as_one_merged_cell(tmp_path):
    from document_translator.services import pdf_table

    tables = pdf_table.extract_pdf_tables(_ruled_page(tmp_path, _P71_RULES, _P71_TEXT_AT))
    header = [cell for cell in tables[0].cells if cell.row == 2 and cell.rect]
    assert len(header) == 7


def test_column_headers_under_a_title_row_keep_their_own_alignment(monkeypatch):
    from types import SimpleNamespace as Cell

    cells = [Cell(id="title", row=1, column=1, rect=(0, 0, 300, 10))]
    cells += [Cell(id=f"h{c}", row=2, column=c, rect=(c * 100 - 100, 10, c * 100, 20)) for c in (1, 2, 3)]
    cells += [Cell(id=f"b{c}", row=3, column=c, rect=(c * 100 - 100, 20, c * 100, 30)) for c in (1, 2, 3)]
    own = {"title": (1, True), "h1": (1, True), "h2": (1, True), "h3": (1, True), "b1": (0, False), "b2": (0, False), "b3": (0, False)}
    monkeypatch.setattr(pdf_inplace, "_source_cell_alignment", lambda page, cell: own[cell.id])
    alignment = pdf_inplace._table_alignment(None, [Cell(cells=cells)])
    assert [alignment[f"h{c}"] for c in (1, 2, 3)] == [(1, True)] * 3  # centred headers, left-aligned body
    assert alignment["b2"] == (0, False)


def test_characters_a_font_lacks_become_their_standard_form():
    simhei = fitz.Font(fontfile=r"C:\Windows\Fonts\simhei.ttf")
    arial = fitz.Font(fontfile=r"C:\Windows\Fonts\arial.ttf")
    assert pdf_inplace._with_font_glyphs("面积为404km²", simhei) == "面积为404km2"
    assert pdf_inplace._with_font_glyphs("• 中央排水渠", simhei) == "· 中央排水渠"
    assert pdf_inplace._with_font_glyphs("PP-142，PP-147", arial) == "PP-142,PP-147"
    assert pdf_inplace._with_font_glyphs("PP-142、PP-147", arial) == "PP-142, PP-147"
    assert pdf_inplace._with_font_glyphs("404km²", arial) == "404km²"  # Arial has it


def test_a_heading_padded_with_spaces_starts_at_its_text(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "      STORM WATER DRAINS", fontsize=12)
    lines, _ = pdf_inplace._visual_lines(page, [])
    assert lines[0].bbox[0] > 90


def test_symbol_font_bullets_start_list_items():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    items = [
        "Elimination of pumping requirement at the intermediate disposal stations, thus saving in higher energy "
        "costs cum maintenance costs and manpower requirements that used to be deployed earlier at the stations.",
        "Existing primary drains, which are presently being used as sullage carriers, will start acting as storm "
        "water channels, which can be perceived as the primary purpose of the project.",
    ]
    y = 100
    for text in items:
        page.insert_text((92, y + 10), "\u2022", fontname="symb", fontsize=11)
        rect = fitz.Rect(108, y, 544, y + 60)
        left = page.insert_textbox(rect, text, fontsize=11, align=3)
        y = rect.y1 - left + 6
    for x in range(12):  # body text elsewhere on the page sets the margins
        page.insert_text((72, 400 + 15 * x), "Body text of the page, set at the left margin of the page.", fontsize=11)
    lines, _ = pdf_inplace._visual_lines(page, [])
    paragraphs = pdf_inplace._segment(1, [l for l in lines if l.bbox[1] < 300], (72.0, 544.0))
    assert [p.text.split()[0] for p in paragraphs] == ["Elimination", "Existing"]


def test_a_superscript_does_not_change_the_line_size():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "approved by PDWP in its 18", fontsize=12)
    page.insert_text((72 + fitz.get_text_length("approved by PDWP in its 18", fontsize=12), 96), "th", fontsize=8)
    lines, _ = pdf_inplace._visual_lines(page, [])
    assert len(lines) == 1 and lines[0].size == 12


def test_roman_numeral_items_keep_their_continuation_lines():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    rows = [("iii.", "Overall financial and administrative management of the project as per rules of the"),
            (None, "Government and Guidelines of the funding agencies/banks."),
            ("iv.", "Review and approval of detailed estimates and variations.")]
    y = 100
    for label, text in rows:
        if label:
            page.insert_text((96 - fitz.get_text_length(label, fontsize=12), y), label, fontsize=12)
        page.insert_text((101, y), text, fontsize=12)
        y += 17
    lines, _ = pdf_inplace._visual_lines(page, [])
    right = lines[0].bbox[2] + 1.0  # the first line is a full justified line
    paragraphs = pdf_inplace._segment(1, lines, (38.0, right))
    assert [len(p.lines) for p in paragraphs] == [2, 1]


def test_text_on_either_side_of_a_ruled_divider_stays_apart():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((100, 100), "1st Half", fontsize=6)
    page.insert_text((128, 100), "2nd Half", fontsize=6)
    page.draw_line((125, 90), (125, 104), color=(0, 0, 0), width=0.5)
    lines, _ = pdf_inplace._visual_lines(page, [])
    assert sorted(line.text for line in lines) == ["1st Half", "2nd Half"]


def _justified(page, x0, x1, y, text, size=12):
    """A line of ``text`` words reaching (nearly) ``x1``, as a justified line does."""
    words, line = (text + " ") * 20, ""
    for word in words.split():
        if fitz.get_text_length(f"{line} {word}".strip(), fontsize=size) > x1 - x0:
            break
        line = f"{line} {word}".strip()
    page.insert_text((x0, y), line, fontsize=size)


def test_first_line_indent_is_measured_against_the_line_above():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    long = "Human Resource Management involves management functions like planning, plus more"
    _justified(page, 90, 544, 120, long)
    page.insert_text((72, 137), "organizing, directing and controlling", fontsize=12)
    lines, _ = pdf_inplace._visual_lines(page, [])
    right = max(line.bbox[2] for line in lines) + 1
    paragraphs = pdf_inplace._segment(1, lines, (38.0, right))  # no margin to measure from
    assert [len(p.lines) for p in paragraphs] == [2]


def test_space_before_a_paragraph_starts_a_new_one():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    text = "Laying of sewer line from Karachi Phattak and Gurumangat Road to Gulshan e Ravi and more"
    for y in (100, 114, 128):
        _justified(page, 180, 524, y, text)
    page.insert_text((180, 145), "Procurement of Works under Engineering", fontsize=12)
    lines, _ = pdf_inplace._visual_lines(page, [])
    right = max(line.bbox[2] for line in lines) + 1
    paragraphs = pdf_inplace._segment(1, lines, (72.0, right))
    assert [len(p.lines) for p in paragraphs] == [3, 1]


def test_fake_bold_glyphs_are_read_once():
    doc = fitz.open()
    page = doc.new_page()
    for dx in (0, 0.4):
        page.insert_text((72 + dx, 100), "MTBM", fontsize=10)
    lines, _ = pdf_inplace._visual_lines(page, [])
    assert [line.text for line in lines] == ["MTBM"]


def test_a_column_of_one_line_rows_is_not_one_cell_paragraph():
    from types import SimpleNamespace

    doc = fitz.open()
    page = doc.new_page()
    names = [f"A10{i} Task name number {i}" for i in range(8)]
    for i, name in enumerate(names):
        page.insert_text((40, 100 + 12 * i), name, fontsize=6)
    cell = SimpleNamespace(id="c", rect=(36, 90, 314, 200), is_empty=False, text="\n".join(names))
    assert pdf_inplace._row_list_cells(page, SimpleNamespace(cells=[cell])) == [cell]
    prose = SimpleNamespace(id="p", rect=(36, 90, 314, 200), is_empty=False, text="one line")
    assert pdf_inplace._row_list_cells(page, SimpleNamespace(cells=[prose])) == []


def test_a_paragraph_broken_by_a_page_is_one_sentence():
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    doc.new_page(width=595, height=842)
    first, second = doc[0], doc[1]
    words = "Construction of sewer through trenchless technology will aid in minimum disruption of traffic"
    for y in (700, 717, 734):
        _justified(first, 72, 523, y, words)
    second.insert_text((72, 90), "resettlement of people or reconstruction of civic infrastructure.", fontsize=12)
    second.insert_text((72, 120), "A new paragraph starts here.", fontsize=12)
    paragraphs = []
    for number, page in ((1, first), (2, second)):
        lines, _ = pdf_inplace._visual_lines(page, [])
        right = max(line.bbox[2] for line in lines) + 1
        for paragraph in pdf_inplace._segment(number, lines, (72.0, right)):
            paragraph.bounds = (72.0, right)
            paragraphs.append(paragraph)
    across = pdf_inplace._across_pages(paragraphs, {1: 842.0, 2: 842.0})
    assert [(paragraphs[h].page_number, paragraphs[t].text[:12]) for h, t in across.items()] == [(1, "resettlement")]


def test_a_line_closing_an_open_bracket_continues_the_paragraph():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 100), "Package C (Disposal Station: 350 Cfs, 10 Pumps of 50 Cfs each with 3 Standby", fontsize=11)
    page.insert_text((144, 116), "Pumps).", fontsize=11)
    lines, _ = pdf_inplace._visual_lines(page, [])
    assert len(pdf_inplace._segment(1, lines, (72.0, 560.0))) == 1


def test_a_numbered_item_hangs_its_wrapped_lines_under_its_text():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((51, 100), "3.", fontsize=12)
    page.insert_text((64.3, 100), "Updated working on MTBM Quantity since insufficient MTBMs were taken", fontsize=12)
    page.insert_text((64.3, 116), "the assignment within stipulated time frame.", fontsize=12)
    lines, _ = pdf_inplace._visual_lines(page, [])
    (paragraph,) = pdf_inplace._segment(1, lines, (51.0, lines[0].bbox[2] + 1))
    label, rest, start = pdf_inplace._hanging_label(paragraph, "3.MTBM数量的更新计算，因为上一版不足，无法完成该任务。")
    assert (label, rest[:4], round(start)) == ("3.", "MTBM", 64)


def test_a_page_stored_rotated_is_translated(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    # Stored turned a quarter, as a landscape page kept in a portrait file.
    page.insert_text((300, 700), "Capital cost estimates", fontsize=12, rotate=90)
    page.set_rotation(90)
    source = tmp_path / "rotated.pdf"
    doc.save(source)

    class Provider:
        provider_name, prompt_version, glossary_version = "fake", "v", "none"

        class config:
            model = "m"
            batch_input_characters = 0

        def translate_batch(self, units):
            from document_translator.core import TranslationResult, sha256_text

            return [TranslationResult(
                unit_id=u.id, translation="资本成本估算", provider="fake", model="m", prompt_version="v",
                glossary_version="none", source_hash=sha256_text(u.source_text), result_hash=sha256_text("资本成本估算"),
                request_count=1, validation_status="valid") for u in units]

    candidate = tmp_path / "out.pdf"
    report = pdf_inplace._translate_in_place(Provider(), source, candidate, source_hash="0" * 64, source_language="en",
                                    target_language="zh", profile=None)
    assert "资本成本估算" in fitz.open(candidate)[0].get_text(), report
    assert not (tmp_path / "out.upright.pdf").exists()


def test_a_value_starting_at_a_column_edge_is_its_own_piece():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((74, 100), "Weight (Battery & Propellers", fontsize=10)
    page.insert_text((240, 100), "1388 g", fontsize=10)
    for y in (140, 160, 180):  # the value column other rows start at
        page.insert_text((240, y), "S-mode: 6 m/s", fontsize=10)
    lines, _ = pdf_inplace._visual_lines(page, [])
    assert "1388 g" in [line.text for line in lines]


def test_key_value_lines_are_separate_entries():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((252, 100), "S-mode: 6 m/s", fontsize=10)
    page.insert_text((252, 114), "P-mode: 5 m/s", fontsize=10)
    lines, _ = pdf_inplace._visual_lines(page, [])
    assert len(pdf_inplace._segment(1, lines, (252.0, lines[0].bbox[2] + 1))) == 2


def test_a_dated_entry_keeps_its_hanging_continuation():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((95, 100), "May 17, 2024: Two surveyors were deployed to undertake the bathymetric survey", fontsize=11)
    text_start = 95 + fitz.get_text_length("May 17, 2024: ", fontsize=11)
    page.insert_text((text_start + 3, 116), "work.", fontsize=11)
    page.insert_text((95, 132), "May 20, 2024: Establishment of Ground Control Points.", fontsize=11)
    lines, _ = pdf_inplace._visual_lines(page, [])
    right = lines[0].bbox[2] + 1
    assert [len(p.lines) for p in pdf_inplace._segment(1, lines, (95.0, right))] == [2, 1]


def test_contents_entries_keep_title_and_page_number():
    match = pdf_inplace._TOC_ENTRY_RE.match("4. Reference Datum ..................................... 6")
    assert match and (match.group("title"), match.group("page")) == ("4. Reference Datum", "6")
    assert pdf_inplace._TOC_ENTRY_RE.match("The total length is 1.5 km") is None


def test_a_page_footer_does_not_hide_a_paragraph_broken_by_the_page():
    doc = fitz.open()
    for _ in range(3):
        doc.new_page(width=595, height=842)
    pages = [doc[i] for i in range(3)]
    words = "Construction of sewer through trenchless technology will aid in minimum disruption of traffic"
    for y in (700, 717, 734):
        _justified(pages[0], 72, 523, y, words)
    pages[1].insert_text((72, 90), "resettlement of people or reconstruction of civic infrastructure.", fontsize=12)
    pages[2].insert_text((72, 90), "Another page of text that ends here.", fontsize=12)
    for number, page in enumerate(pages, 1):  # footer at 91% of the page height
        page.insert_text((450, 768), f"Page {number} of 3", fontsize=9)
    paragraphs = []
    for number, page in enumerate(pages, 1):
        lines, _ = pdf_inplace._visual_lines(page, [])
        right = max(line.bbox[2] for line in lines if line.bbox[1] < 760) + 1
        for paragraph in pdf_inplace._segment(number, lines, (72.0, right)):
            paragraph.bounds = (72.0, right)
            paragraphs.append(paragraph)
    across = pdf_inplace._across_pages(paragraphs, {1: 842.0, 2: 842.0, 3: 842.0})
    assert [(paragraphs[h].page_number, paragraphs[t].text[:12]) for h, t in across.items()] == [(1, "resettlement")]


def test_two_columns_are_read_one_after_the_other():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    left = ["Sewerage system from LARECHS Colony to", "Gulshan-e-Ravi, Lahore through trenchless", "technology for the city."]
    right = ["14,165 Million PKR approved as the", "project cost by the forum in 2020 for", "the whole of the works."]
    for i, (a, b) in enumerate(zip(left, right)):
        page.insert_text((46, 120 + 16 * i), a, fontsize=11)
        page.insert_text((330, 120 + 16 * i), b, fontsize=11)
    page.insert_text((46, 300), "Funding agency text in the left column only", fontsize=11)
    page.insert_text((330, 300), "And more in the right column at this height", fontsize=11)
    lines, _ = pdf_inplace._visual_lines(page, [])
    ordered = [line.text[:8] for line in pdf_inplace._reading_order(lines, (46.0, 560.0))]
    assert ordered.index("Gulshan-") < ordered.index("14,165 M")


def test_a_table_border_under_a_line_of_text_is_not_its_underline(tmp_path):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((340, 309), "the mentioned lengths will be used:", fontsize=11)
    shape = page.new_shape()
    for rect in (
        (356.4, 312.3, 471.3, 312.8),  # the table's top border
        (355.9, 312.8, 356.3, 326.1),  # its column rules
        (471.3, 312.8, 471.8, 326.1),
    ):
        shape.draw_rect(fitz.Rect(rect))  # each piece its own path, as Word draws them
        shape.finish(fill=(0, 0, 0), color=None)
    shape.commit()
    lines, _ = pdf_inplace._visual_lines(page, [])
    (paragraph,) = pdf_inplace._segment(1, lines, (340.0, 560.0))
    paragraph.bounds = (340.0, 560.0)
    rules = pdf_inplace._rule_drawings(page)
    before = len(page.get_drawings())
    pdf_inplace._refit_underline(page, paragraph, "将使用以下直径：", 11, fitz.Rect(340, 298, 560, 312), rules)
    assert len(page.get_drawings()) == before


def test_a_coloured_bullet_does_not_colour_the_line():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "•", fontsize=11, color=(0.1, 0.68, 0.89))
    page.insert_text((90, 100), "Jacking Shafts: 82", fontsize=11, color=(0, 0, 0))
    lines, _ = pdf_inplace._visual_lines(page, [])
    assert lines[0].color & 0xFFFFFF == 0


def test_text_of_separate_boxes_stays_apart_and_a_box_is_one_text():
    doc = fitz.open()
    page = doc.new_page(width=960, height=540)
    for x0, words in ((35, ("1. Collection", "System")), (252, ("2. Conveyance", "System"))):
        page.draw_rect(fitz.Rect(x0, 276, x0 + 180, 366), color=(1, 1, 1), fill=(0.11, 0.38, 0.58))
        for k, word in enumerate(words):
            width = fitz.get_text_length(word, fontsize=16)
            page.insert_text((x0 + 90 - width / 2, 310 + 22 * k), word, fontsize=16, color=(1, 1, 1))
    lines, _ = pdf_inplace._visual_lines(page, [])
    paragraphs = pdf_inplace._segment(1, lines, (35.0, 900.0))
    assert sorted(p.text for p in paragraphs) == ["1. Collection System", "2. Conveyance System"]


def test_a_table_cell_keeps_its_text_colour():
    from document_translator.services import pdf_table

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((100, 100), "B. Contents of Tender Documents", fontsize=11, color=(1, 1, 1))
    assert pdf_table._cell_color(page, fitz.Rect(90, 85, 400, 105)) == (1.0, 1.0, 1.0)


def test_a_highlighted_phrase_is_marked_for_translation():
    highlights = [{"rect": (100, 100, 200, 112), "text": "free of cost", "color": (1, 1, 0)}]
    marked = pdf_inplace._mark_highlights("Contractor\nfree of cost, and no", highlights, (90, 90, 300, 130))
    assert marked == "Contractor\n\u27e6H\u27e7free of cost\u27e6/H\u27e7, and no"
    assert pdf_inplace._strip_highlight_markers(marked) == "Contractor\nfree of cost, and no"
    assert pdf_inplace._mark_highlights("other text", highlights, (400, 400, 500, 500)) == "other text"


def test_a_row_broken_by_a_page_fills_the_first_cell_first(tmp_path):
    from types import SimpleNamespace

    head = SimpleNamespace(id="h", rect=(0, 0, 200, 60), source_font_size=10.0, is_empty=False, text="x")
    text = "根据我们的经验，我们认为对于小直径的管道而言，这样的强度过高且不经济。" * 3
    first, rest = pdf_inplace._fill_continued(head, text)
    assert first and rest and first + rest == text.replace(" ", "") or (first + rest).replace(" ", "") == text
    assert len(first) > len(text) // 3  # filled, not cut by the English proportion
    short = "强度过高。"
    assert pdf_inplace._fill_continued(head, short) == (short, "")


def test_key_value_lines_in_a_cell_stay_one_per_line():
    text = "Generated: yes\nMethod: Inverse Distance\nWeighting\nMerge Tiles: yes"
    assert pdf_inplace._structure_cell_text(text) == "Generated: yes\nMethod: Inverse Distance Weighting\nMerge Tiles: yes"
