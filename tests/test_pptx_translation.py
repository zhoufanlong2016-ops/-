from __future__ import annotations

from zipfile import ZipFile
from xml.etree import ElementTree as ET

from document_translator.services.pptx_translation import PptxTranslationService
from document_translator.services.pptx_layout import PptxLayoutService
from document_translator.core import TranslationResult, sha256_text
import pytest


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
        return [TranslationResult(
            unit_id=unit.id, translation=self.translation, provider="fake", model="fake",
            prompt_version="test", glossary_version="none", source_hash=sha256_text(unit.source_text),
            result_hash=sha256_text(self.translation), request_count=1, validation_status="valid",
        ) for unit in units]


def _pending_names():
    from document_translator.core import DocumentFormat, DocumentLocation, TranslationUnit, generate_unit_id
    pending = []
    for index, text in enumerate(("RAVI Rd.", "SHAREEF COLONY DS")):
        paragraph = ET.fromstring(f'<a:p xmlns:a="{A}"><a:r><a:t>{text}</a:t></a:r></a:p>')
        data = dict(document_hash="a" * 64, format=DocumentFormat.PPTX,
                    location=DocumentLocation(part="slide1", object_id=str(index)),
                    source_language="en", target_language="zh-CN", source_text=text, protected_tokens=[])
        pending.append((paragraph, paragraph.findall(f".//{{{A}}}t"), TranslationUnit(id=generate_unit_id(**data), **data)))
    return pending


def test_pptx_reorders_by_stable_id_before_xml_mutation():
    class Provider(_Provider):
        def translate_batch(self, units):
            results = []
            for unit in units:
                self.translation = "译文（" + unit.source_text + "）"
                results.extend(super().translate_batch([unit]))
            return results[::-1]
    pending = _pending_names()
    PptxTranslationService(Provider(""))._apply_batch(pending)
    assert [nodes[0].text for _, nodes, _ in pending] == ["译文（RAVI Rd.）", "译文（SHAREEF COLONY DS）"]


@pytest.mark.parametrize("defect", ["duplicate", "unknown", "source_hash", "result_hash", "name", "protected"])
def test_pptx_invalid_result_leaves_entire_batch_xml_untouched(defect):
    pending = _pending_names()
    if defect == "protected":
        p, nodes, unit = pending[1]
        unit = unit.model_copy(update={"source_text": "SHAREEF COLONY DS 5%", "protected_tokens": ["5%"]})
        pending[1] = (p, nodes, unit)
    before = [ET.tostring(p) for p, _, _ in pending]

    class Provider(_Provider):
        def translate_batch(self, units):
            results = []
            for unit in units:
                self.translation = "译文（" + unit.source_text + "）"
                results.extend(super().translate_batch([unit]))
            last = results[1]
            if defect == "duplicate":
                results[1] = results[0]
            elif defect == "unknown":
                results[1] = last.model_copy(update={"unit_id": "unknown"})
            elif defect in {"source_hash", "result_hash"}:
                results[1] = last.model_copy(update={defect: "0" * 64})
            else:
                text = "译文" if defect == "name" else last.translation.replace("5%", "6%")
                results[1] = last.model_copy(update={"translation": text, "result_hash": sha256_text(text)})
            return results

    with pytest.raises(ValueError):
        PptxTranslationService(Provider(""))._apply_batch(pending)
    assert [ET.tostring(p) for p, _, _ in pending] == before


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
