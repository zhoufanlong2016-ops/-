from hashlib import sha256
from xml.dom import minidom
from zipfile import ZipFile

import pytest

from document_translator.adapters.docx import (
    extract_paragraph_translation_units,
    extract_translation_units,
    rewrite_docx,
    rewrite_docx_paragraphs,
)
from document_translator.core import DocumentFormat, TranslationResult, sha256_text


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def xml_part(root: str, text: str) -> bytes:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<w:{root} xmlns:w="{W}"><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:{root}>'''.encode()


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.docx"
    document = f'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="{W}"><w:body>
<w:p><w:r><w:t>Hello</w:t><w:t/></w:r><w:del><w:r><w:t>Deleted</w:t></w:r></w:del>
<w:r><w:instrText>PAGE</w:instrText><w:t>Visible result</w:t></w:r></w:p>
</w:body></w:document>'''.encode()
    with ZipFile(path, "w") as package:
        package.comment = b"keep comment"
        for name, value in {
            "[Content_Types].xml": b"content-types",
            "_rels/.rels": b"relationships",
            "word/document.xml": document,
            "word/header1.xml": xml_part("hdr", "Header"),
            "word/footer1.xml": xml_part("ftr", "Footer"),
            "word/footnotes.xml": xml_part("footnotes", "Footnote"),
            "word/endnotes.xml": xml_part("endnotes", "Endnote"),
            "word/media/image.png": b"binary-media",
        }.items():
            package.writestr(name, value)
    return path


def test_extracts_supported_parts_with_stable_ids(source) -> None:
    first = extract_translation_units(source, source_language="en", target_language="zh-CN")
    second = extract_translation_units(source, source_language="en", target_language="zh-CN")

    assert [unit.source_text for unit in first] == ["Hello", "Visible result", "Header", "Footer", "Footnote", "Endnote"]
    assert [unit.location.part for unit in first] == [
        "word/document.xml", "word/document.xml", "word/header1.xml", "word/footer1.xml",
        "word/footnotes.xml", "word/endnotes.xml",
    ]
    assert first == second
    assert all(unit.format == DocumentFormat.DOCX for unit in first)
    assert all(unit.document_hash == sha256(source.read_bytes()).hexdigest() for unit in first)


def test_rewrite_changes_only_supported_text_parts(source, tmp_path) -> None:
    before = source.read_bytes()
    units = extract_translation_units(source)
    destination = tmp_path / "translated.docx"
    translations = {unit.id: f"T-{index}" for index, unit in enumerate(units)}
    translations[units[0].id] = " Leading text "

    rewrite_docx(source, destination, units, translations)

    assert source.read_bytes() == before
    with ZipFile(source) as original, ZipFile(destination) as output:
        assert original.comment == output.comment
        assert original.read("word/media/image.png") == output.read("word/media/image.png")
        assert original.read("_rels/.rels") == output.read("_rels/.rels")
        document = minidom.parseString(output.read("word/document.xml"))
        text = [node.firstChild.data if node.firstChild else "" for node in document.getElementsByTagNameNS(W, "t")]
        assert text == [" Leading text ", "", "Deleted", "T-1"]
        assert document.getElementsByTagNameNS(W, "t")[0].getAttribute("xml:space") == "preserve"
        assert b"T-2" in output.read("word/header1.xml")
        assert b"T-3" in output.read("word/footer1.xml")
        assert b"T-4" in output.read("word/footnotes.xml")
        assert b"T-5" in output.read("word/endnotes.xml")


def test_rewrite_rejects_incomplete_or_unknown_mapping_without_output(source, tmp_path) -> None:
    units = extract_translation_units(source)
    destination = tmp_path / "translated.docx"
    with pytest.raises(ValueError, match="missing"):
        rewrite_docx(source, destination, units, {units[0].id: "T"})
    with pytest.raises(ValueError, match="unknown"):
        rewrite_docx(source, destination, units, {**{unit.id: "T" for unit in units}, "unknown": "T"})
    assert not destination.exists()


def test_rewrite_rejects_invalid_result_and_illegal_xml_text(source, tmp_path) -> None:
    units = extract_translation_units(source)
    destination = tmp_path / "translated.docx"
    invalid = TranslationResult(
        unit_id=units[1].id, translation="T", provider="test", model="test", prompt_version="1",
        glossary_version="1", source_hash=sha256_text(units[0].source_text), result_hash=sha256_text("T"),
        request_count=1, validation_status="valid",
    )
    translations = {unit.id: "T" for unit in units}
    translations[units[0].id] = invalid
    with pytest.raises(ValueError):
        rewrite_docx(source, destination, units, translations)
    translations[units[0].id] = "bad\x00text"
    with pytest.raises(ValueError, match="XML-illegal"):
        rewrite_docx(source, destination, units, translations)
    assert not destination.exists()


def test_rewrite_refuses_overwrite_and_same_path(source, tmp_path) -> None:
    units = extract_translation_units(source)
    translations = {unit.id: "T" for unit in units}
    with pytest.raises(ValueError, match="must differ"):
        rewrite_docx(source, source, units, translations)
    destination = tmp_path / "existing.docx"
    destination.write_bytes(b"keep")
    with pytest.raises(FileExistsError):
        rewrite_docx(source, destination, units, translations)
    assert destination.read_bytes() == b"keep"


def test_same_style_adjacent_runs_are_one_unit_and_keep_run_structure(tmp_path) -> None:
    source = tmp_path / "runs.docx"
    xml = f'''<w:document xmlns:w="{W}"><w:body><w:p>
<w:r><w:rPr><w:b/></w:rPr><w:t>Hel</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>lo</w:t></w:r>
<w:r><w:rPr><w:i/></w:rPr><w:t>Italic</w:t></w:r><w:r><w:instrText>PAGE</w:instrText><w:t>Page</w:t></w:r>
</w:p></w:body></w:document>'''.encode()
    with ZipFile(source, "w") as package:
        package.writestr("word/document.xml", xml)
    units = extract_translation_units(source)
    assert [unit.source_text for unit in units] == ["Hello", "Italic", "Page"]
    assert units[0].location.node_ids == ["w:t:0", "w:t:1"]

    destination = tmp_path / "runs-translated.docx"
    rewrite_docx(source, destination, units, {unit.id: f"T-{index}" for index, unit in enumerate(units)})
    document = minidom.parseString(ZipFile(destination).read("word/document.xml"))
    texts = [node.firstChild.data if node.firstChild else "" for node in document.getElementsByTagNameNS(W, "t")]
    assert texts == ["T-0", "", "T-1", "T-2"]
    assert len(document.getElementsByTagNameNS(W, "rPr")) == 3


def test_rewrite_removes_soft_break_after_last_visible_text(tmp_path) -> None:
    source = tmp_path / "soft-break.docx"
    xml = f'''<w:document xmlns:w="{W}"><w:body><w:p>
<w:r><w:t>Text</w:t></w:r><w:r><w:br/></w:r><w:r><w:t> </w:t></w:r>
</w:p></w:body></w:document>'''.encode()
    with ZipFile(source, "w") as package:
        package.writestr("word/document.xml", xml)
    units = extract_translation_units(source)
    destination = tmp_path / "soft-break-translated.docx"
    rewrite_docx(source, destination, units, {units[0].id: "Translated"})
    document = minidom.parseString(ZipFile(destination).read("word/document.xml"))
    assert not document.getElementsByTagNameNS(W, "br")


def test_paragraph_priority_rewrite_preserves_runs_and_removes_tail_soft_break(tmp_path) -> None:
    source = tmp_path / "paragraphs.docx"
    xml = f'''<w:document xmlns:w="{W}"><w:body><w:p>
<w:r><w:rPr><w:b/></w:rPr><w:t>First </w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t>paragraph</w:t></w:r>
<w:r><w:br/></w:r></w:p><w:p><w:r><w:t>Second</w:t></w:r></w:p>
</w:body></w:document>'''.encode()
    with ZipFile(source, "w") as package:
        package.writestr("word/document.xml", xml)
    units = extract_paragraph_translation_units(source, source_language="en", target_language="zh-CN")
    assert [unit.source_text for unit in units] == ["First paragraph", "Second"]
    assert units[0].location.node_ids == ["w:t:0", "w:t:1"]

    destination = tmp_path / "paragraphs-translated.docx"
    rewrite_docx_paragraphs(source, destination, units, {units[0].id: "第一段", units[1].id: "第二段"})
    document = minidom.parseString(ZipFile(destination).read("word/document.xml"))
    texts = [node.firstChild.data if node.firstChild else "" for node in document.getElementsByTagNameNS(W, "t")]
    assert texts == ["第一段", "", "第二段"]
    assert not document.getElementsByTagNameNS(W, "br")
    assert len(document.getElementsByTagNameNS(W, "rPr")) == 3
    fonts = document.getElementsByTagNameNS(W, "rFonts")
    assert all(node.getAttributeNS(W, "eastAsia") == "SimHei" for node in fonts)


def test_docx_extraction_marks_numbers_codes_and_urls_as_protected(tmp_path) -> None:
    source = tmp_path / "rules.docx"
    xml = xml_part("document", "ISO 9001 at 105+820, 5%: https://example.test/spec")
    with ZipFile(source, "w") as package:
        package.writestr("word/document.xml", xml)

    unit = extract_translation_units(source, source_language="en", target_language="zh-CN")[0]
    assert unit.protected_tokens == ["ISO 9001", "105+820", "5%", "https://example.test/spec"]
