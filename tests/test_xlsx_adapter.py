from __future__ import annotations

import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from document_translator.adapters.xlsx import XlsxAdapterError, read_xlsx, rewrite_xlsx
from document_translator.core import DocumentFormat


def write_xlsx(path: Path, text: str = "阀门") -> None:
    files = {
        "[Content_Types].xml": "<Types/>",
        "docProps/core.xml": "<core>unchanged</core>",
        "xl/workbook.xml": '''<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Visible" sheetId="1" r:id="rId1"/><sheet name="Hidden" sheetId="2" state="hidden" r:id="rId2"/></sheets></workbook>''',
        "xl/_rels/workbook.xml.rels": '''<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Target="worksheets/sheet2.xml"/></Relationships>''',
        f"xl/sharedStrings.xml": f"<sst count=\"2\" uniqueCount=\"1\"><si><t>{text}</t><phoneticPr/></si></sst>",
        "xl/styles.xml": "<styleSheet xmlns=\"http://schemas.openxmlformats.org/spreadsheetml/2006/main\"><fonts count=\"1\"><font><name val=\"Calibri\"/></font></fonts><cellXfs count=\"2\"><xf numFmtId=\"0\" fontId=\"0\"/><xf numFmtId=\"0\" fontId=\"0\" applyFont=\"1\"/></cellXfs></styleSheet>",
        "xl/worksheets/sheet1.xml": "<worksheet><sheetData><row r=\"1\"><c r=\"A1\" s=\"1\" t=\"s\"><v>0</v></c><c r=\"B1\" t=\"s\"><v>0</v></c><c r=\"C1\"><f>SUM(1,2)</f><v>3</v></c><c r=\"D1\" t=\"inlineStr\"><is><t>管道</t></is></c></row></sheetData></worksheet>",
        "xl/worksheets/sheet2.xml": "<worksheet><sheetData><row r=\"1\"><c r=\"A1\" t=\"s\"><v>0</v></c></row></sheetData></worksheet>",
    }
    with zipfile.ZipFile(path, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value.encode("utf-8"))


def test_reads_visible_shared_and_inline_strings_but_skips_formula_and_hidden_sheet(tmp_path) -> None:
    source = tmp_path / "source.xlsx"
    write_xlsx(source)

    result = read_xlsx(source, source_language="zh-CN", target_language="en")

    assert [unit.location.object_id for unit in result.units] == ["cell:A1", "cell:B1", "cell:D1"]
    assert [unit.source_text for unit in result.units] == ["阀门", "阀门", "管道"]
    assert all(unit.format is DocumentFormat.XLSX for unit in result.units)
    assert result.skipped == ("HIDDEN_SHEET:Hidden",)


def test_rewrite_isolates_shared_strings_and_preserves_formula_and_untouched_parts(tmp_path) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "translated.xlsx"
    write_xlsx(source)
    result = read_xlsx(source, source_language="zh-CN", target_language="en")
    translations = dict(zip(
        [unit.id for unit in result.units],
        ["Valve", "Gate", "Pipeline"],
        strict=True,
    ))

    rewrite_xlsx(
        source,
        output,
        result.units,
        translations,
        source_language="zh-CN",
        target_language="en",
    )

    with zipfile.ZipFile(source) as original, zipfile.ZipFile(output) as translated:
        assert original.read("docProps/core.xml") == translated.read("docProps/core.xml")
        sheet = ET.fromstring(translated.read("xl/worksheets/sheet1.xml"))
        cells = {cell.get("r"): cell for cell in sheet.findall(".//c")}
        assert cells["A1"].findtext("v") == "1"
        assert cells["B1"].findtext("v") == "2"
        assert cells["C1"].findtext("f") == "SUM(1,2)"
        assert cells["D1"].findtext("is/t") == "Pipeline"
        strings = ET.fromstring(translated.read("xl/sharedStrings.xml"))
        assert [node.findtext("t") for node in strings.findall("si")] == ["阀门", "Valve", "Gate"]
        assert strings.get("count") == "2"
        assert strings.get("uniqueCount") == "3"


def test_rewrite_rejects_missing_mapping_without_creating_destination(tmp_path) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "translated.xlsx"
    write_xlsx(source)
    units = read_xlsx(source).units

    with pytest.raises(XlsxAdapterError, match="units or translations"):
        rewrite_xlsx(source, output, units, {})

    assert not output.exists()


def test_rewrite_applies_cjk_font_to_translated_text_cells(tmp_path) -> None:
    source = tmp_path / "source.xlsx"
    output = tmp_path / "translated.xlsx"
    write_xlsx(source)
    result = read_xlsx(source, source_language="en", target_language="zh-CN")
    rewrite_xlsx(source, output, result.units, {unit.id: "中文" for unit in result.units},
                 source_language="en", target_language="zh-CN")

    with zipfile.ZipFile(output) as package:
        styles = ET.fromstring(package.read("xl/styles.xml"))
        names = [node.find("{*}name").get("val") for node in styles.findall("{*}fonts/{*}font")]
        assert "SimHei" in names
        simhei_id = names.index("SimHei")
        sheet = ET.fromstring(package.read("xl/worksheets/sheet1.xml"))
        cells = {cell.get("r"): cell for cell in sheet.findall(".//{*}c")}
        assert int(cells["A1"].get("s")) >= 2
        xfs = styles.findall("{*}cellXfs/{*}xf")
        assert int(xfs[int(cells["A1"].get("s"))].get("fontId")) == simhei_id
        assert xfs[int(cells["A1"].get("s"))].get("applyFont") == "1"


def test_rewrite_keeps_excel_extension_namespace_prefixes(tmp_path) -> None:
    """mc:Ignorable prefixes must still be declared after XML writeback."""
    source = tmp_path / "source.xlsx"
    output = tmp_path / "translated.xlsx"
    files = {
        "[Content_Types].xml": "<Types/>",
        "xl/workbook.xml": '<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Visible" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        "xl/sharedStrings.xml": '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="1" uniqueCount="1"><si><t>Valve</t><phoneticPr fontId="0" type="noConversion"/></si></sst>',
        "xl/styles.xml": '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:x14ac="http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac" mc:Ignorable="x14ac xr2"><fonts count="1"><font><name val="Calibri"/></font></fonts><cellXfs count="1"><xf numFmtId="0" fontId="0"/></cellXfs></styleSheet>',
        "xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" xmlns:xr="http://schemas.microsoft.com/office/spreadsheetml/2014/revision" mc:Ignorable="xr xr2 xr3" xr:uid="{00000000-0000-0000-0000-000000000000}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData></worksheet>',
    }
    with zipfile.ZipFile(source, "w") as archive:
        for name, value in files.items():
            archive.writestr(name, value.encode("utf-8"))

    result = read_xlsx(source, source_language="en", target_language="zh-CN")
    rewrite_xlsx(source, output, result.units, {result.units[0].id: "阀门"},
                 source_language="en", target_language="zh-CN")

    with zipfile.ZipFile(output) as package:
        for name in ("xl/styles.xml", "xl/worksheets/sheet1.xml"):
            raw = package.read(name).decode("utf-8")
            assert 'xmlns:xr2="http://schemas.microsoft.com/office/spreadsheetml/2015/revision2"' in raw
        assert 'xmlns:xr3="http://schemas.microsoft.com/office/spreadsheetml/2016/revision3"' in package.read("xl/worksheets/sheet1.xml").decode("utf-8")
        shared = ET.fromstring(package.read("xl/sharedStrings.xml"))
        assert shared.findall("{*}si")[-1].find("{*}phoneticPr") is None


def test_xlsx_extraction_marks_numbers_and_codes_as_protected(tmp_path) -> None:
    source = tmp_path / "source.xlsx"
    write_xlsx(source, "Valve ISO 9001 at 105+820 and 5%")
    result = read_xlsx(source, source_language="en", target_language="zh-CN")

    assert result.units[0].protected_tokens == ["ISO 9001", "105+820", "5%"]
