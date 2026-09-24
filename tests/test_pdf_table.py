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
