from __future__ import annotations

import json
import sys
import types
from types import SimpleNamespace

import fitz

from document_translator.services.pdf_hybrid_parser import parse_pdf
from document_translator.services.mineru_pdf import MinerUPdfTranslationService
from document_translator.core import sha256_text


def _make_source(path):
    document = fitz.open()
    page = document.new_page(width=300, height=400)
    page.insert_text((40, 60), "Chapter 1 General Provisions", fontsize=14)
    page.insert_text((40, 90), "This is the first line of a paragraph.", fontsize=10)
    page.insert_text((40, 104), "This is the continuation line.", fontsize=10)
    page.insert_text((40, 160), "Article 2 Scope", fontsize=12)
    origin = fitz.Point(220, 260)
    page.insert_text(origin, "Rotated label", fontsize=9, morph=(origin, fitz.Matrix(30)))
    document.save(path)
    document.close()


def test_native_parser_keeps_stable_geometry_and_semantic_boundaries(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source(source)

    first = parse_pdf(source, parser="native")
    second = parse_pdf(source, parser="native")

    assert first.source_hash == second.source_hash
    assert [line.id for line in first.native_lines] == [line.id for line in second.native_lines]
    assert first.rotated_pages == (1,)
    assert any(abs(line.angle) > 1 for line in first.native_lines)
    assert [block.kind for block in first.semantic_blocks[:3]] == ["heading", "paragraph", "heading"]
    assert first.boundary_map["1"][0]["first_line_text"] == "This is the first line of a paragraph."


def test_auto_parser_uses_explicit_mineru_middle_json_and_native_geometry(tmp_path):
    source = tmp_path / "source.pdf"
    _make_source(source)
    mineru = tmp_path / "middle_json.json"
    mineru.write_text(
        json.dumps(
            {
                "schema": "docvortex.middle",
                "pages": [
                    {
                        "page_idx": 0,
                        "blocks": [
                            {"type": "title", "bbox": [35, 45, 260, 68], "content": "Chapter 1 General Provisions"},
                            {"type": "text", "bbox": [35, 80, 270, 110], "content": "This is the first line of a paragraph.\nThis is the continuation line."},
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = parse_pdf(source, parser="auto", mineru_output=mineru)

    assert result.parser == "mineru"
    assert result.mineru_status == "loaded"
    assert [block.source for block in result.semantic_blocks] == ["mineru", "mineru"]
    assert result.semantic_blocks[0].line_ids
    assert result.semantic_blocks[0].line_ids[0] == result.native_lines[0].id


def test_mineru_service_translates_structured_block_and_publishes_original_pdf(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    destination = tmp_path / "translated.pdf"
    report = tmp_path / "translated.json"
    document = fitz.open()
    page = document.new_page(width=300, height=400)
    page.insert_text((40, 60), "Hello PDF", fontsize=11)
    document.save(source)
    document.close()

    parsed = {
        "schema": "docvortex.middle",
        "pages": [{"page_idx": 0, "blocks": [{"type": "text", "content": "Hello PDF", "bbox": [40, 45, 100, 65]}]}],
    }

    def fake_parse(_path, *, tier, ocr_mode):
        assert (tier, ocr_mode) == ("flash", "txt")
        return SimpleNamespace(middle_json=parsed)

    def fake_render(middle_json, *, layout):
        assert str(layout).lower().endswith("original")
        output = fitz.open()
        page = output.new_page(width=300, height=400)
        page.insert_text((40, 60), middle_json["pages"][0]["blocks"][0]["content"], fontsize=11)
        data = output.tobytes()
        output.close()
        return data

    mineru = types.ModuleType("mineru")
    parser_module = types.ModuleType("mineru.parser")
    render_module = types.ModuleType("mineru.render")
    parser_module.parse = fake_parse
    render_module.PdfLayout = SimpleNamespace(ORIGINAL="original")
    render_module.render_pdf = fake_render
    mineru.parser = parser_module
    mineru.render = render_module
    monkeypatch.setitem(sys.modules, "mineru", mineru)
    monkeypatch.setitem(sys.modules, "mineru.parser", parser_module)
    monkeypatch.setitem(sys.modules, "mineru.render", render_module)

    class Provider:
        provider_name = "fake"
        config = SimpleNamespace(model="fake-model")
        prompt_version = "test"
        glossary_version = "none"

        def translate_batch(self, units):
            from document_translator.core import TranslationResult

            return [
                TranslationResult(
                    unit_id=unit.id,
                    translation="你好 PDF",
                    provider="fake",
                    model="fake-model",
                    prompt_version="test",
                    glossary_version="none",
                    source_hash=sha256_text(unit.source_text),
                    result_hash=sha256_text("你好 PDF"),
                    request_count=1,
                    validation_status="valid",
                )
                for unit in units
            ]

    output, preflight, report_path = MinerUPdfTranslationService(Provider()).translate_file(
        source,
        destination,
        source_language="en",
        target_language="zh",
        report_path=report,
    )

    assert output == destination.resolve()
    assert preflight.classification == "A"
    assert parsed["pages"][0]["blocks"][0]["content"] == "你好 PDF"
    assert destination.is_file()
    assert json.loads(report_path.read_text(encoding="utf-8"))["run"]["engine"] == "mineru4"
