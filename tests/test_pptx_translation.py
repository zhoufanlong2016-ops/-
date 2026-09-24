from __future__ import annotations

from types import SimpleNamespace
from zipfile import ZipFile
from xml.etree import ElementTree as ET

from document_translator.services.pptx_translation import PptxTranslationService
from document_translator.services.pptx_layout import PptxLayoutService


A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def _write_pptx(path, text: str) -> None:
    xml = f'''<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="{A}">
      <p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:rPr sz="2400"/><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld>
    </p:sld>'''
    with ZipFile(path, "w") as package:
        package.writestr("ppt/slides/slide1.xml", xml)


class _Provider:
    def __init__(self, translation: str):
        self.translation = translation

    def translate_batch(self, units):
        self.units = list(units)
        return [SimpleNamespace(translation=self.translation) for _ in units]


def test_english_to_chinese_pptx_is_not_filtered_and_uses_cjk_font(tmp_path) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, "Project title")

    provider = _Provider("项目标题")
    count = PptxTranslationService(provider).translate_file(
        source, output, source_language="en", target_language="zh-CN",
    )

    assert count == 1
    with ZipFile(output) as package:
        root = ET.fromstring(package.read("ppt/slides/slide1.xml"))
        assert root.find(f".//{{{A}}}t").text == "项目标题"
        ea = root.find(f".//{{{A}}}ea")
        latin = root.find(f".//{{{A}}}latin")
        assert ea is not None and ea.get("typeface") == "SimHei"
        assert latin is not None and latin.get("typeface") == "Arial"
    assert provider.units[0].protected_tokens == []


def test_pptx_marks_engineering_values_as_protected_and_creates_missing_run_properties(tmp_path) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, "ISO 9001 - 5% at 105+820")
    # Deliberately remove rPr to verify that font policy creates it.
    with ZipFile(source) as package:
        xml = package.read("ppt/slides/slide1.xml").replace(b'<a:rPr sz="2400"/>', b"")
    with ZipFile(source, "w") as package:
        package.writestr("ppt/slides/slide1.xml", xml)

    provider = _Provider("ISO 9001 - 5% 位于 105+820")
    PptxTranslationService(provider).translate_file(source, output, source_language="en", target_language="zh-CN")

    assert provider.units[0].protected_tokens == ["ISO 9001", "5%", "105+820"]
    with ZipFile(output) as package:
        root = ET.fromstring(package.read("ppt/slides/slide1.xml"))
        assert root.find(f".//{{{A}}}rPr/{{{A}}}ea").get("typeface") == "SimHei"


def test_english_to_chinese_pptx_preserves_mixed_chinese_paragraph(tmp_path) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, "EPC施工总承包")

    provider = _Provider("unexpected translation")
    count = PptxTranslationService(provider).translate_file(
        source, output, source_language="en", target_language="zh-CN",
    )

    assert count == 0
    with ZipFile(output) as package:
        root = ET.fromstring(package.read("ppt/slides/slide1.xml"))
        assert root.find(f".//{{{A}}}t").text == "EPC施工总承包"


def test_chinese_to_english_pptx_applies_font_to_untouched_acronym(tmp_path) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, "DAAB")

    provider = _Provider("unused")
    assert PptxTranslationService(provider).translate_file(
        source, output, source_language="zh", target_language="en",
    ) == 0
    with ZipFile(output) as package:
        root = ET.fromstring(package.read("ppt/slides/slide1.xml"))
        assert root.find(f".//{{{A}}}rPr/{{{A}}}latin").get("typeface") == "Arial"


def test_pptx_translation_never_scales_font_from_character_count_again(tmp_path) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, "短文")

    PptxTranslationService(_Provider("A deliberately much longer translated sentence.")).translate_file(
        source, output, source_language="zh", target_language="en",
    )

    with ZipFile(output) as package:
        root = ET.fromstring(package.read("ppt/slides/slide1.xml"))
        assert root.find(f".//{{{A}}}rPr").get("sz") == "2400"


def test_powerpoint_layout_script_is_generic_and_measurement_based() -> None:
    script = PptxLayoutService()._script()
    assert "Fit-Measured" in script
    assert "Fit-TableMeasured" in script
    assert "BoundHeight" in script
    assert "Expand-BodyHeightSafely" in script
    assert "Type -eq 17" in script
    assert "slideIndex -eq" not in script


def test_pptx_translation_never_scales_font_from_character_count(tmp_path) -> None:
    source = tmp_path / "source.pptx"
    output = tmp_path / "output.pptx"
    _write_pptx(source, "短文")

    PptxTranslationService(_Provider("A deliberately much longer translated sentence.")).translate_file(
        source, output, source_language="zh", target_language="en",
    )

    with ZipFile(output) as package:
        root = ET.fromstring(package.read("ppt/slides/slide1.xml"))
        assert root.find(f".//{{{A}}}rPr").get("sz") == "2400"
