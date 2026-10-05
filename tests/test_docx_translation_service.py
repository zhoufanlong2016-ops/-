from zipfile import ZipFile

from document_translator.core import TranslationResult, sha256_text
from document_translator.services.docx_translation import DocxTranslationService, write_docx_comparison_report


W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


class FakeProvider:
    provider_name = "fake"
    prompt_version = "v1"
    glossary_version = "none"

    class config:
        model = "fake-model"

    def translate_unit(self, unit):
        value = f"translated:{unit.source_text}"
        return TranslationResult(
            unit_id=unit.id, translation=value, provider=self.provider_name, model=self.config.model,
            prompt_version=self.prompt_version, glossary_version=self.glossary_version,
            source_hash=sha256_text(unit.source_text), result_hash=sha256_text(value),
            request_count=1, validation_status="valid",
        )


class DefectiveFakeProvider(FakeProvider):
    def translate_unit(self, unit):
        value = "this方案—A3‑grade soil"
        return TranslationResult(
            unit_id=unit.id, translation=value, provider=self.provider_name, model=self.config.model,
            prompt_version=self.prompt_version, glossary_version=self.glossary_version,
            source_hash=sha256_text(unit.source_text), result_hash=sha256_text(value),
            request_count=1, validation_status="valid",
        )


class RecordingFakeProvider(FakeProvider):
    def __init__(self):
        self.calls = []

    def translate_unit(self, unit):
        self.calls.append(unit.source_text)
        return super().translate_unit(unit)


def test_service_writes_new_paragraph_priority_docx(tmp_path) -> None:
    source = tmp_path / "source.docx"
    source_xml = f'''<w:document xmlns:w="{W}"><w:body><w:p>
<w:r><w:t>Hello </w:t></w:r><w:r><w:t>world</w:t></w:r>
</w:p></w:body></w:document>'''.encode()
    with ZipFile(source, "w") as package:
        package.writestr("word/document.xml", source_xml)
    destination = tmp_path / "translated.docx"

    outcome = DocxTranslationService(FakeProvider()).translate_file(
        source, destination, source_language="en", target_language="zh-CN",
    )

    assert len(outcome.units) == 1
    with ZipFile(destination) as package:
        assert b"translated:Hello world" in package.read("word/document.xml")
    with ZipFile(source) as package:
        assert b"Hello " in package.read("word/document.xml")


def test_service_reports_english_typography_and_cjk_defects_but_keeps_draft(tmp_path) -> None:
    source = tmp_path / "source.docx"
    with ZipFile(source, "w") as package:
        package.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>Source</w:t></w:r></w:p></w:body></w:document>'.encode())
    destination = tmp_path / "draft.docx"

    outcome = DocxTranslationService(DefectiveFakeProvider()).translate_file(
        source, destination, source_language="zh-CN", target_language="en",
    )

    assert destination.exists()
    assert outcome.quality_issues == ("CJK_RESIDUE",)


def test_comparison_report_contains_matched_source_translation_and_hashes(tmp_path) -> None:
    source = tmp_path / "source.docx"
    with ZipFile(source, "w") as package:
        package.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>Source</w:t></w:r></w:p></w:body></w:document>'.encode())
    outcome = DocxTranslationService(FakeProvider()).translate_file(
        source, tmp_path / "translated.docx", source_language="en", target_language="zh-CN",
    )
    report = tmp_path / "comparison.json"
    write_docx_comparison_report(report, outcome)

    import json
    body = json.loads(report.read_text(encoding="utf-8"))
    assert body["format"] == "document-translator-docx-comparison-v1"
    assert body["records"][0]["source_text"] == "Source"
    assert body["records"][0]["translation"] == "translated:Source"
    assert len(body["records"][0]["unit_id"]) == 64


def test_long_paragraph_is_translated_as_complete_source_segments(tmp_path) -> None:
    source = tmp_path / "source.docx"
    text = "甲句包含足够多的文字以触发拆分。乙句也包含足够多的文字以触发拆分。丙句同样完整。"
    with ZipFile(source, "w") as package:
        package.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'.encode())
    provider = RecordingFakeProvider()
    outcome = DocxTranslationService(provider, max_segment_chars=20).translate_file(
        source, tmp_path / "translated.docx", source_language="zh-CN", target_language="en",
    )

    assert provider.calls == ["甲句包含足够多的文字以触发拆分。", "乙句也包含足够多的文字以触发拆分。", "丙句同样完整。"]
    assert outcome.results[0].request_count == 3
    assert outcome.results[0].translation.count("translated:") == 3


def test_default_segment_limit_keeps_legal_sentences_below_the_model_context_window(tmp_path) -> None:
    source = tmp_path / "source.docx"
    text = "甲" * 100 + "。" + "乙" * 61 + "。"
    with ZipFile(source, "w") as package:
        package.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'.encode())
    provider = RecordingFakeProvider()

    DocxTranslationService(provider).translate_file(
        source, tmp_path / "translated.docx", source_language="zh-CN", target_language="en",
    )

    assert provider.calls == ["甲" * 100 + "。", "乙" * 61 + "。"]


def test_english_text_leaves_a_chinese_latin_font():
    import xml.etree.ElementTree as ET

    from document_translator.adapters import docx as adapter

    w = adapter._WORD_NAMESPACE
    root = ET.fromstring(
        f'<w:body xmlns:w="{w}"><w:p><w:r><w:rPr><w:rFonts w:hint="eastAsia" w:ascii="宋体" w:hAnsi="宋体" w:eastAsia="宋体"/></w:rPr><w:t>x</w:t></w:r>'
        f'<w:r><w:rPr><w:rFonts w:asciiTheme="minorEastAsia" w:hAnsiTheme="minorEastAsia"/></w:rPr><w:t>y</w:t></w:r></w:p></w:body>'
    )
    texts = list(root.iter(f"{{{w}}}t"))
    adapter._apply_latin_font_policy(root, texts, "Average side length")
    fonts = list(root.iter(f"{{{w}}}rFonts"))
    assert fonts[0].get(f"{{{w}}}ascii") == "Times New Roman" and fonts[0].get(f"{{{w}}}eastAsia") == "宋体"
    assert fonts[0].get(f"{{{w}}}hint") is None
    assert fonts[1].get(f"{{{w}}}asciiTheme") == "minorHAnsi"
