from __future__ import annotations

import json

import fitz

from document_translator.services.pdf_layout import (
    NumberingProfile,
    build_layout_contracts,
    chinese_numeral_to_int,
    load_numbering_profile,
    normalize_document_reference_translation,
    normalize_numbered_translation,
    parse_document_reference,
    parse_numbered_source,
)


def test_arbitrary_angle_drawing_is_not_reclassified_as_prose(tmp_path):
    path = tmp_path / "drawing.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((80, 80), "Drawing legend:", fontsize=14)
    origin = fitz.Point(150, 150)
    page.insert_text(origin, "Pipe route", morph=(origin, fitz.Matrix(30)))
    doc.save(path)
    doc.close()
    assert build_layout_contracts(path) == ()


def test_chinese_ordinals_and_structural_parser_are_document_neutral() -> None:
    assert chinese_numeral_to_int("七") == 7
    assert chinese_numeral_to_int("二十一") == 21
    assert parse_numbered_source("第七条信息追踪").body == "信息追踪"
    assert parse_numbered_source("第一章 总 则").ordinal == 1
    reference = parse_document_reference("某机构〔2031〕18 号")
    assert reference is not None
    assert (reference.prefix, reference.year, reference.serial) == ("某机构", "2031", "18")


def test_numbered_translation_uses_profile_not_sample_text() -> None:
    profile = NumberingProfile(
        target_language="en",
        chapter_label="Section",
        article_label="Clause",
        chapter_numbering="roman",
        article_numbering="arabic",
    )
    assert normalize_numbered_translation("第一章 总则", "Chapter One General Provisions", profile) == "Section I General Provisions"
    assert normalize_numbered_translation("第七条信息追踪", "Article Seven Information Tracking", profile) == "Clause 7 Information Tracking"


def test_document_reference_rebuilds_numeric_fields_without_inventing_issuer() -> None:
    profile = NumberingProfile(target_language="en")
    assert normalize_document_reference_translation("某机构〔2031〕18号", "Some Institute〔2031〕18 号", profile) == "Some Institute [2031] No. 18"
    # brackets dropped by the provider are restored; other numbers are not matched
    assert normalize_document_reference_translation("某机构〔2031〕18号", "Some Institute 2031 No. 18", profile) == "Some Institute [2031] No. 18"
    assert normalize_document_reference_translation("某机构〔2031〕18号", "Some Institute 2031 No. 180", profile) == "Some Institute 2031 No. 180"
    # An unresolved CJK issuer is deliberately left for strict language
    # validation/glossary review rather than silently transliterated.
    assert normalize_document_reference_translation("某机构〔2031〕18号", "某机构〔2031〕18号", profile) == "某机构〔2031〕18号"


def test_style_profile_is_loaded_from_json(tmp_path) -> None:
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps({"chapter": {"label": "Part", "numbering": "roman"}, "article": {"label": "Clause", "numbering": "arabic"}}),
        encoding="utf-8",
    )
    profile = load_numbering_profile(path, target_language="en")
    assert (profile.chapter_label, profile.chapter_numbering, profile.article_label) == ("Part", "roman", "Clause")


def test_layout_contract_merges_same_baseline_fragments(tmp_path) -> None:
    source = tmp_path / "source.pdf"
    doc = fitz.open()
    page = doc.new_page(width=300, height=400)
    fontfile = r"C:\Windows\Fonts\Noto Sans SC (TrueType).otf"
    page.insert_text((110, 100), "第一章", fontsize=16, fontfile=fontfile, fontname="NotoSansSC")
    page.insert_text((165, 100), "总则", fontsize=16, fontfile=fontfile, fontname="NotoSansSC")
    page.insert_text((40, 150), "第七条信息追踪", fontsize=12, fontfile=fontfile, fontname="NotoSansSC")
    doc.save(source)
    doc.close()

    contracts = build_layout_contracts(source)
    headings = [item for item in contracts if item.role in {"chapter_heading", "article_heading"}]
    assert [(item.role, item.line_count) for item in headings] == [("chapter_heading", 1), ("article_heading", 1)]
