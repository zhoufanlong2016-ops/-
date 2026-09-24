import csv
import zipfile

import pytest

from document_translator.services import Glossary, GlossaryEntry, GlossaryError, load_glossary


def write_csv(path, rows, *, bom=False):
    with path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)


def write_xlsx(path, sheet_rows):
    shared = []
    indexes = {}
    for row in sheet_rows:
        for value in row:
            if value not in indexes:
                indexes[value] = len(shared)
                shared.append(value)
    shared_xml = "".join(f"<si><t>{value}</t></si>" for value in shared)
    row_xml = []
    for row_number, row in enumerate(sheet_rows, start=1):
        cells = "".join(
            f'<c r="{chr(65 + column)}{row_number}" t="s"><v>{indexes[value]}</v></c>'
            for column, value in enumerate(row)
        )
        row_xml.append(f'<row r="{row_number}">{cells}</row>')
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/workbook.xml",
            '<workbook xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Terms" sheetId="1" r:id="rId1"/></sheets></workbook>',
        )
        archive.writestr(
            "xl/_rels/workbook.xml.rels",
            '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
        )
        archive.writestr("xl/sharedStrings.xml", f"<sst>{shared_xml}</sst>")
        archive.writestr("xl/worksheets/sheet1.xml", f"<worksheet><sheetData>{''.join(row_xml)}</sheetData></worksheet>")


def test_csv_bom_optional_columns_and_deterministic_matching(tmp_path):
    path = tmp_path / "terms.csv"
    write_csv(path, [[" Source ", " TARGET ", "note"], ["pump", "泵", "x"], ["pump station", "泵站", "y"], ["pump", "泵", "z"]], bom=True)

    glossary = load_glossary(path)

    assert [(entry.source, entry.target) for entry in glossary.entries] == [("pump", "泵"), ("pump station", "泵站")]
    assert [entry.source for entry in glossary.entries_for("pump station and pump")] == ["pump station", "pump"]
    assert glossary.entries_for("Pump") == ()


def test_matching_accepts_hyphen_and_simple_english_plural_variants() -> None:
    glossary = Glossary(
        entries=(
            GlossaryEntry(source="control panel", target="控制柜"),
            GlossaryEntry(source="launch shaft", target="始发井"),
        ),
        version="v1",
    )

    assert [entry.target for entry in glossary.entries_for("control-panel supply")] == ["控制柜"]
    assert [entry.target for entry in glossary.entries_for("two launch shafts")] == ["始发井"]


def test_matching_prefers_longer_cjk_phrase() -> None:
    glossary = Glossary(
        entries=(
            GlossaryEntry(source="污水管", target="sewer pipe"),
            GlossaryEntry(source="污水管道", target="sewer pipeline"),
        ),
        version="v1",
    )

    assert [entry.source for entry in glossary.entries_for("污水管道")] == ["污水管道"]


@pytest.mark.parametrize(
    "rows",
    [
        [["term", "target"], ["pump", "泵"]],
        [["source", "target"], ["", "泵"]],
        [["source", "target"], ["pump", ""]],
        [["source", "target"], ["pump", "泵"], ["pump", "水泵"]],
    ],
)
def test_csv_validation_errors(tmp_path, rows):
    path = tmp_path / "invalid.csv"
    write_csv(path, rows)
    with pytest.raises(GlossaryError):
        Glossary.load(path)


def test_xlsx_loading_from_standard_library_zip_fixture(tmp_path):
    path = tmp_path / "terms.xlsx"
    write_xlsx(path, [["source", "target", "comment"], ["sewer", "污水管", "ordinary"], ["sewer pipe", "污水管道", "ordinary"]])

    glossary = Glossary.load(path)

    assert [(entry.source, entry.target) for entry in glossary.entries_for("sewer pipe")] == [("sewer pipe", "污水管道"), ("sewer", "污水管")]


def test_suffix_and_version_behavior(tmp_path):
    unsupported = tmp_path / "terms.txt"
    unsupported.write_text("source,target\npump,泵\n", encoding="utf-8")
    with pytest.raises(GlossaryError):
        Glossary.load(unsupported)

    first = tmp_path / "first.csv"
    reordered = tmp_path / "reordered.csv"
    changed = tmp_path / "changed.csv"
    write_csv(first, [["source", "target"], ["pump", "泵"], ["valve", "阀门"]])
    write_csv(reordered, [["source", "target"], ["valve", "阀门"], ["pump", "泵"]])
    write_csv(changed, [["source", "target"], ["pump", "水泵"], ["valve", "阀门"]])

    assert Glossary.load(first).version == Glossary.load(reordered).version
    assert Glossary.load(first).version != Glossary.load(changed).version
