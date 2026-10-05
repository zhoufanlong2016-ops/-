from __future__ import annotations

from pathlib import Path

import pytest

from document_translator.services.pdf_table import (
    PdfTableFitError,
    PdfTableMappingError,
    PdfTableTranslation,
    extract_pdf_tables,
    render_table_translations,
    validate_table_translations,
)
from document_translator.services.pdf_table import _normalise_render_text


def test_table_mapping_rejects_name_loss_before_render():
    from document_translator.services.pdf_table import PdfTable, PdfTableCell, validate_pdf_table_translations
    cell = PdfTableCell("c", 1, 1, 1, 1, (0, 0, 100, 100), "ZAFAR ALI ROAD DS")
    table = PdfTable(1, 1, (0, 0, 100, 100), 1, 1, (cell,))
    for validate, source in ((validate_table_translations, table), (validate_pdf_table_translations, (table,))):
        with pytest.raises(PdfTableMappingError, match="PROPER_NAME_MISSING"):
            validate(source, {"c": "扎法尔阿里路下游"})
        assert validate(source, {"c": "扎法尔阿里路（ZAFAR ALI ROAD）下游"})["c"]


def _fontfile() -> Path:
    for candidate in (Path(r"C:\Windows\Fonts\NotoSans-Regular.ttf"), Path(r"C:\Windows\Fonts\arial.ttf")):
        if candidate.is_file():
            return candidate
    if not Path(r"C:\Windows\Fonts\arial.ttf").is_file():
        pytest.skip("Arial is not available in this Windows test environment")
    return Path(r"C:\Windows\Fonts\arial.ttf")


def _make_table_pdf(path: Path, *, narrow: bool = False) -> None:
    import fitz

    document = fitz.open()
    page = document.new_page(width=260 if narrow else 360, height=220)
    x_right = 100 if narrow else 320
    for y in (30, 62, 105, 150):
        page.draw_line((20, y), (x_right, y))
    page.draw_line((20, 30), (20, 150))
    page.draw_line((x_right, 30), (x_right, 150))
    page.draw_line(((x_right + 20) / 2, 30), ((x_right + 20) / 2, 150))
    page.insert_text((25, 50), "Name", fontsize=10)
    page.insert_text((25, 82), "Multi\nline", fontsize=10)
    page.insert_text(((x_right + 20) / 2 + 5, 82), "Source", fontsize=10)
    document.save(path)
    document.close()


def test_normalise_render_text_reflows_plain_paragraph_but_preserves_lists() -> None:
    assert _normalise_render_text("first line\nsecond line") == "first line second line"
    assert _normalise_render_text("1. first\ncontinuation\n2. second") == "1. first continuation\n2. second"


def test_extracts_logical_grid_with_stable_ids_and_multiline_text(tmp_path):
    source = tmp_path / "source.pdf"
    _make_table_pdf(source)

    tables = extract_pdf_tables(source)

    assert len(tables) == 1
    table = tables[0]
    assert table.id == "pdf:p1:t1"
    assert (table.row_count, table.column_count) == (3, 2)
    assert [cell.id for cell in table.cells] == [
        f"pdf:p1:t1:r{row}:c{column}"
        for row in range(1, 4)
        for column in range(1, 3)
    ]
    assert table.cells[2].text == "Multi\nline"
    assert table.cells[3].text == "Source"
    assert table.cells[5].is_empty
    assert all(cell.rect is not None for cell in table.cells)


def test_validation_rejects_missing_and_duplicate_cell_ids(tmp_path):
    source = tmp_path / "source.pdf"
    _make_table_pdf(source)
    table = extract_pdf_tables(source)[0]
    complete = {cell.id: (cell.text or "") for cell in table.cells}
    complete[table.cells[0].id] = "名称"

    missing = dict(complete)
    missing.pop(table.cells[-1].id)
    with pytest.raises(PdfTableMappingError, match="missing cell translations"):
        validate_table_translations(table, missing)

    duplicate = [PdfTableTranslation(cell_id, text) for cell_id, text in complete.items()]
    duplicate.append(PdfTableTranslation(table.cells[0].id, "重复"))
    with pytest.raises(PdfTableMappingError, match="duplicate translation ID"):
        validate_table_translations(table, duplicate)


def test_render_redacts_only_text_preserves_vector_lines_and_embeds_font(tmp_path):
    import fitz

    source = tmp_path / "source.pdf"
    destination = tmp_path / "translated.pdf"
    _make_table_pdf(source)
    table = extract_pdf_tables(source)[0]
    translations = {cell.id: (f"Translated {cell.row}-{cell.column}" if not cell.is_empty else "") for cell in table.cells}

    report = render_table_translations(source, destination, translations, fontfile=_fontfile())

    assert report.destination == destination
    assert report.table_count == 1
    assert report.rendered_cell_count == 3
    assert min(report.font_size_map.values()) >= 6
    # Every engine repairs the text layer after rendering: Arial shares one
    # glyph between " " and U+00A0 and between "-" and U+00AD.
    from document_translator.services.pdf_pipeline import repair_pdf_text_cmaps

    repair_pdf_text_cmaps(destination)
    source_doc = fitz.open(source)
    output_doc = fitz.open(destination)
    try:
        assert len(output_doc[0].get_drawings()) == len(source_doc[0].get_drawings())
        assert "Translated 1-1" in output_doc[0].get_text()
        assert "Name" not in output_doc[0].get_text()
        assert any(font[4].startswith("pdfTable") for font in output_doc[0].get_fonts(full=True))
        assert all(float(span["size"]) >= 6 for block in output_doc[0].get_text("dict")["blocks"] if block.get("type") == 0 for line in block["lines"] for span in line["spans"] if span.get("text", "").strip())
    finally:
        source_doc.close()
        output_doc.close()


def test_render_fails_if_translation_cannot_fit_at_floor_and_does_not_publish(tmp_path):
    source = tmp_path / "narrow.pdf"
    destination = tmp_path / "translated.pdf"
    _make_table_pdf(source, narrow=True)
    table = extract_pdf_tables(source)[0]
    translations = {cell.id: ("This is a deliberately very long translation that cannot fit in this tiny cell" if not cell.is_empty else "") for cell in table.cells}

    with pytest.raises(PdfTableFitError, match="minimum font size"):
        render_table_translations(
            source,
            destination,
            translations,
            fontfile=_fontfile(),
            minimum_font_size=6,
            initial_font_size=6,
        )
    assert not destination.exists()


def test_render_allows_short_header_in_compact_cell(tmp_path):
    source = tmp_path / "compact-header.pdf"
    destination = tmp_path / "compact-header-translated.pdf"
    _make_table_pdf(source, narrow=True)
    table = extract_pdf_tables(source)[0]
    translations = {
        cell.id: ("序号" if cell.row == 1 and cell.column == 1 else ("值" if not cell.is_empty else ""))
        for cell in table.cells
    }

    report = render_table_translations(
        source,
        destination,
        translations,
        fontfile=_fontfile(),
        minimum_font_size=6,
        initial_font_size=6,
    )

    assert report.rendered_cell_count == 3
    assert destination.exists()


def test_chinese_is_set_one_and_a_half_lines_apart_when_it_fits():
    from document_translator.services import pdf_table

    font = r"C:\Windows\Fonts\simhei.ttf"
    text = "根据我们的经验，这样的强度过高且不经济。\n是否允许承包商通过降低强度来优化设计？"
    ascender = pdf_table.cached_font(font).ascender
    roomy = pdf_table.chinese_line_height(300, 100, text, fontfile=font, fontname="F", fontsize=10)
    assert roomy is not None and abs(roomy * ascender - 1.5) < 1e-6
    tight = pdf_table.chinese_line_height(300, 22, text, fontfile=font, fontname="F", fontsize=10)
    assert tight is None or tight * ascender < 1.5
    assert pdf_table.chinese_line_height(300, 100, "English only", fontfile=font, fontname="F", fontsize=10) is None


def test_reserve_includes_descent_so_full_cell_keeps_wide_spacing() -> None:
    # A cell filled to its last line at 1.5 spacing was refused by the
    # font's descent and written at the cramped pitch instead.
    import fitz

    from document_translator.services.pdf_table import _with_reserve, chinese_line_height

    font = r"C:\Windows\Fonts\simhei.ttf"
    if not Path(font).exists():
        pytest.skip("SimHei not installed")
    for lines in range(1, 8):
        text = "\n".join(["汉字测试"] * lines)
        rect = fitz.Rect(0, 0, 200, (lines - 1) * 1.5 * 11 + 11 + 0.6)
        lineheight = chinese_line_height(rect.width, rect.height, text, fontfile=font, fontname="p", fontsize=11)
        assert lineheight is not None
        page = fitz.open().new_page()
        result = page.insert_textbox(_with_reserve(rect, text, font, "p", 11, lineheight), text, fontfile=font, fontname="p", fontsize=11, lineheight=lineheight)
        assert result >= 0


def test_header_row_without_column_rules_gets_its_columns_back() -> None:
    # White labels on a blue band, no rules between them: found as one cell
    # across the table, translated as "全名公司名称电子邮件地址".
    import fitz

    from document_translator.services.pdf_table import PdfTableCell, _split_unruled_spans

    page = fitz.open().new_page(width=600, height=200)
    for x, text in ((52, "Full Name"), (182, "Company Name"), (402, "Email Address"), (52, "Engineering Team"), (182, "CRBC"), (402, "a@b.com")):
        page.insert_text((x, 30 if text in ("Full Name", "Company Name", "Email Address") else 45), text, fontsize=8)

    def cell(row, column, rect, text):
        return PdfTableCell(f"c{row}{column}", 1, 1, row, column, rect, text)

    cells = (
        cell(1, 1, (50, 20, 560, 34), "Full Name Company Name Email Address"), cell(1, 2, None, ""), cell(1, 3, None, ""),
        cell(2, 1, (50, 34, 180, 48), "Engineering Team"), cell(2, 2, (180, 34, 400, 48), "CRBC"), cell(2, 3, (400, 34, 560, 48), "a@b.com"),
    )
    split = _split_unruled_spans(cells, page)
    assert [(c.text, c.rect) for c in split[:3]] == [
        ("Full Name", (50, 20, 180, 34)), ("Company Name", (180, 20, 400, 34)), ("Email Address", (400, 20, 560, 34)),
    ]
    # One title across the columns is a real merged cell and stays.
    page2 = fitz.open().new_page(width=600, height=200)
    page2.insert_text((150, 30), "Attendees of the Pre-Bid Meeting", fontsize=8)
    page2.insert_text((52, 45), "Engineering Team", fontsize=8)
    merged = (cell(1, 1, (50, 20, 560, 34), "Attendees of the Pre-Bid Meeting"), *cells[1:])
    assert _split_unruled_spans(merged, page2) is merged


def test_closing_punctuation_run_never_starts_a_line() -> None:
    from document_translator.services.pdf_table import _wrap_atomic_phrases

    font = r"C:\Windows\Fonts\simhei.ttf"
    if not Path(font).exists():
        pytest.skip("SimHei not installed")
    text = "气味控制和通风系统的总数应读作：29（二十九）。招标文件中任何位置的气味控制和通风系统数量均应读作29（二十九）。"
    wrapped = _wrap_atomic_phrases(text, fontfile=font, fontname="p", fontsize=11, max_width=252.52)
    assert all(not line.startswith(("）", "。")) for line in wrapped.split("\n"))


def test_compact_lines_never_overlap() -> None:
    from document_translator.services.pdf_table import cached_font, compact_line_height

    font = r"C:\Windows\Fonts\simhei.ttf"
    if not Path(font).exists():
        pytest.skip("SimHei not installed")
    lineheight = compact_line_height(108, 50, "子条款（健康与安全\n义务）\n监控报告第4个\n要点", fontfile=font, fontname="p", fontsize=11)
    f = cached_font(font)
    assert lineheight is None or lineheight * f.ascender >= f.ascender - f.descender
