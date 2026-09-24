"""Conservative OOXML extraction and rewrite for ordinary XLSX cell strings."""

from __future__ import annotations

import copy
import hashlib
import os
import posixpath
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
from xml.etree import ElementTree as ET

from document_translator.core import (
    DocumentFormat,
    DocumentLocation,
    TranslationResult,
    TranslationUnit,
    generate_unit_id,
    validate_result_for_unit,
)
from document_translator.font_policy import CJK_FONT, contains_cjk
from document_translator.translation_rules import rule_protected_tokens

_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_SST_PART = "xl/sharedStrings.xml"
_STYLES_PART = "xl/styles.xml"

# Preserve the prefixes referenced by mc:Ignorable in Excel's extension
# attributes.  ElementTree otherwise renames them to ns0/ns1 while leaving
# the Ignorable value unchanged, producing a package that Excel repairs.
for _prefix, _uri in {
    "": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": _REL_NS,
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "x14ac": "http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac",
    "x16r2": "http://schemas.microsoft.com/office/spreadsheetml/2015/02/main",
    "xr": "http://schemas.microsoft.com/office/spreadsheetml/2014/revision",
    "xr2": "http://schemas.microsoft.com/office/spreadsheetml/2015/revision2",
    "xr3": "http://schemas.microsoft.com/office/spreadsheetml/2016/revision3",
    "x14": "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main",
    "x15": "http://schemas.microsoft.com/office/spreadsheetml/2010/11/main",
}.items():
    ET.register_namespace(_prefix, _uri)

_OOXML_NS = {
    "r": _REL_NS,
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "x14": "http://schemas.microsoft.com/office/spreadsheetml/2009/9/main",
    "x14ac": "http://schemas.microsoft.com/office/spreadsheetml/2009/9/ac",
    "x15": "http://schemas.microsoft.com/office/spreadsheetml/2010/11/main",
    "x16r2": "http://schemas.microsoft.com/office/spreadsheetml/2015/02/main",
    "xr": "http://schemas.microsoft.com/office/spreadsheetml/2014/revision",
    "xr2": "http://schemas.microsoft.com/office/spreadsheetml/2015/revision2",
    "xr3": "http://schemas.microsoft.com/office/spreadsheetml/2016/revision3",
}


def _serialize_xml(root: ET.Element) -> bytes:
    """Serialize while retaining prefixes referenced by Excel extension data."""
    text = ET.tostring(root, encoding="unicode")
    head_end = text.find(">")
    if head_end < 0:
        return text.encode("utf-8")
    head = text[:head_end]
    additions = []
    for prefix, uri in _OOXML_NS.items():
        if f"xmlns:{prefix}=" not in head:
            additions.append(f' xmlns:{prefix}="{uri}"')
    if additions:
        text = text[:head_end] + "".join(additions) + text[head_end:]
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>\r\n" + text).encode("utf-8")


class XlsxAdapterError(ValueError):
    """Raised when an XLSX package cannot be safely translated."""


@dataclass(frozen=True, slots=True)
class XlsxReadResult:
    units: tuple[TranslationUnit, ...]
    skipped: tuple[str, ...]


def _package_bytes(path: Path) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise XlsxAdapterError(f"cannot read XLSX source: {path}") from exc
    if not zipfile.is_zipfile(path):
        raise XlsxAdapterError("source is not an XLSX ZIP package")
    return raw


def _sheet_parts(package: zipfile.ZipFile) -> list[tuple[str, str, bool]]:
    try:
        workbook = ET.fromstring(package.read("xl/workbook.xml"))
        relationships = ET.fromstring(package.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError) as exc:
        raise XlsxAdapterError("XLSX workbook relationships are invalid") from exc
    targets = {
        item.get("Id"): item.get("Target")
        for item in relationships.findall("{*}Relationship")
        if item.get("Id") and item.get("Target")
    }
    parts: list[tuple[str, str, bool]] = []
    for sheet in workbook.findall(".//{*}sheets/{*}sheet"):
        name = sheet.get("name")
        rel_id = sheet.get(f"{{{_REL_NS}}}id")
        target = targets.get(rel_id)
        if not name or not target:
            raise XlsxAdapterError("XLSX worksheet relationship cannot be resolved")
        part = posixpath.normpath(target.lstrip("/")) if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
        parts.append((name, part, sheet.get("state", "visible") == "visible"))
    if not parts:
        raise XlsxAdapterError("XLSX workbook has no worksheets")
    return parts


def _shared_strings(package: zipfile.ZipFile) -> tuple[ET.Element | None, list[ET.Element]]:
    try:
        root = ET.fromstring(package.read(_SST_PART))
    except KeyError:
        return None, []
    except ET.ParseError as exc:
        raise XlsxAdapterError("XLSX shared strings are malformed") from exc
    return root, list(root.findall("{*}si"))


def _plain_text(node: ET.Element) -> str | None:
    direct = node.findall("{*}t")
    if len(direct) != 1:
        return None
    # Excel may attach phoneticPr to an otherwise ordinary shared string;
    # it is metadata, not a rich-text run and must not make the cell skipped.
    if any(child.tag.split("}")[-1] not in {"t", "phoneticPr"} for child in node):
        return None
    return direct[0].text or ""


def _cell_text(cell: ET.Element, strings: Sequence[ET.Element]) -> tuple[str, str] | None:
    if cell.find("{*}f") is not None:
        return None
    cell_type = cell.get("t")
    if cell_type == "s":
        raw_index = cell.findtext("{*}v")
        try:
            index = int(raw_index or "")
            text = _plain_text(strings[index])
        except (IndexError, ValueError) as exc:
            raise XlsxAdapterError("XLSX shared string reference is invalid") from exc
        return (f"sst:{index}", text) if text is not None else None
    if cell_type == "inlineStr":
        inline = cell.find("{*}is")
        text = _plain_text(inline) if inline is not None else None
        return ("inline", text) if text is not None else None
    return None


def read_xlsx(
    path: str | Path,
    *,
    source_language: str = "auto",
    target_language: str = "en",
    include_hidden_sheets: bool = False,
) -> XlsxReadResult:
    """Extract ordinary visible string cells without touching the source package."""
    source = Path(path)
    raw = _package_bytes(source)
    document_hash = hashlib.sha256(raw).hexdigest()
    units: list[TranslationUnit] = []
    skipped: list[str] = []
    try:
        with zipfile.ZipFile(_BytesReader(raw)) as package:
            _, strings = _shared_strings(package)
            for sheet_name, part, visible in _sheet_parts(package):
                if not visible and not include_hidden_sheets:
                    skipped.append(f"HIDDEN_SHEET:{sheet_name}")
                    continue
                try:
                    root = ET.fromstring(package.read(part))
                except (KeyError, ET.ParseError) as exc:
                    raise XlsxAdapterError(f"XLSX worksheet is invalid: {part}") from exc
                for cell in root.findall(".//{*}sheetData/{*}row/{*}c"):
                    reference = cell.get("r")
                    if not reference:
                        raise XlsxAdapterError(f"XLSX cell is missing its reference in {part}")
                    value = _cell_text(cell, strings)
                    if value is None:
                        continue
                    storage, text = value
                    if not text.strip():
                        continue
                    data = dict(
                        document_hash=document_hash,
                        format=DocumentFormat.XLSX,
                        location=DocumentLocation(part=part, object_id=f"cell:{reference}", node_ids=[storage]),
                        source_language=source_language,
                        target_language=target_language,
                        source_text=text,
                        protected_tokens=rule_protected_tokens(text),
                        style_signature=cell.get("s", ""),
                        context_before="",
                        context_after="",
                    )
                    units.append(TranslationUnit(id=generate_unit_id(**data), **data))
    except zipfile.BadZipFile as exc:
        raise XlsxAdapterError("source is not a readable XLSX ZIP package") from exc
    return XlsxReadResult(units=tuple(units), skipped=tuple(skipped))


def extract_xlsx_units(path: str | Path, **kwargs) -> list[TranslationUnit]:
    return list(read_xlsx(path, **kwargs).units)


def _translation_text(unit: TranslationUnit, value: str | TranslationResult) -> str:
    if isinstance(value, TranslationResult):
        errors = validate_result_for_unit(unit, value)
        if errors:
            raise XlsxAdapterError(f"invalid result for {unit.id}: {', '.join(errors)}")
        text = value.translation
    elif isinstance(value, str):
        text = value
    else:
        raise XlsxAdapterError(f"invalid translation value for {unit.id}")
    if not text.strip() or any(ord(char) < 0x20 and char not in "\t\n\r" for char in text):
        raise XlsxAdapterError(f"invalid translation text for {unit.id}")
    return text


def _ensure_cjk_font(styles_root: ET.Element) -> int:
    fonts = styles_root.find("{*}fonts")
    if fonts is None:
        raise XlsxAdapterError("XLSX styles are missing the fonts collection")
    font_nodes = fonts.findall("{*}font")
    for index, font in enumerate(font_nodes):
        name = font.find("{*}name")
        if name is not None and name.get("val") == CJK_FONT:
            return index
    if not font_nodes:
        raise XlsxAdapterError("XLSX styles contain no font definitions")
    font = copy.deepcopy(font_nodes[0])
    name = font.find("{*}name")
    if name is None:
        name = ET.Element("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}name")
        font.insert(0, name)
    name.set("val", CJK_FONT)
    fonts.append(font)
    fonts.set("count", str(len(fonts.findall("{*}font"))))
    return len(fonts.findall("{*}font")) - 1


def _cjk_style_index(
    styles_root: ET.Element,
    original_style: int,
    cjk_font_id: int,
    cache: dict[int, int],
) -> int:
    if original_style in cache:
        return cache[original_style]
    cell_xfs = styles_root.find("{*}cellXfs")
    if cell_xfs is None:
        raise XlsxAdapterError("XLSX styles are missing the cellXfs collection")
    xfs = cell_xfs.findall("{*}xf")
    if original_style < 0 or original_style >= len(xfs):
        raise XlsxAdapterError(f"XLSX cell style index is invalid: {original_style}")
    clone = copy.deepcopy(xfs[original_style])
    clone.set("fontId", str(cjk_font_id))
    # An XF that changes its font must explicitly apply the font component;
    # Excel otherwise may report the workbook as needing repair.
    clone.set("applyFont", "1")
    cell_xfs.append(clone)
    cell_xfs.set("count", str(len(cell_xfs.findall("{*}xf"))))
    value = len(cell_xfs.findall("{*}xf")) - 1
    cache[original_style] = value
    return value


def rewrite_xlsx(
    source_path: str | Path,
    destination_path: str | Path,
    units: Sequence[TranslationUnit],
    translations: Mapping[str, str | TranslationResult],
    *,
    source_language: str = "auto",
    target_language: str = "en",
    include_hidden_sheets: bool = False,
) -> None:
    """Write a new XLSX package after strictly revalidating every target cell."""
    source, destination = Path(source_path), Path(destination_path)
    if source.resolve() == destination.resolve():
        raise XlsxAdapterError("source and destination must differ")
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise XlsxAdapterError(f"destination directory does not exist: {destination.parent}")
    raw = _package_bytes(source)
    current = read_xlsx(
        source,
        source_language=source_language,
        target_language=target_language,
        include_hidden_sheets=include_hidden_sheets,
    )
    expected = {unit.id: unit for unit in current.units}
    supplied = {unit.id: unit for unit in units}
    if set(expected) != set(supplied) or set(translations) != set(expected):
        raise XlsxAdapterError("units or translations do not match current XLSX text cells")
    for unit_id, current_unit in expected.items():
        if supplied[unit_id] != current_unit:
            raise XlsxAdapterError("units do not match current XLSX text cells")

    rewritten: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(_BytesReader(raw)) as package:
            sst_root, strings = _shared_strings(package)
            styles_root = ET.fromstring(package.read(_STYLES_PART)) if _STYLES_PART in package.namelist() else None
            cjk_font_id: int | None = None
            cjk_style_cache: dict[int, int] = {}
            styles_changed = False
            cells_by_part: dict[str, dict[str, TranslationUnit]] = {}
            for unit in current.units:
                cells_by_part.setdefault(unit.location.part, {})[unit.location.object_id.removeprefix("cell:")] = unit
            shared_changed = False
            for part, targets in cells_by_part.items():
                root = ET.fromstring(package.read(part))
                for cell in root.findall(".//{*}sheetData/{*}row/{*}c"):
                    unit = targets.get(cell.get("r", ""))
                    if unit is None:
                        continue
                    text = _translation_text(unit, translations[unit.id])
                    if contains_cjk(text):
                        if styles_root is None:
                            raise XlsxAdapterError("XLSX output needs styles.xml for CJK font policy")
                        if cjk_font_id is None:
                            cjk_font_id = _ensure_cjk_font(styles_root)
                        original_style = int(cell.get("s", "0"))
                        cell.set("s", str(_cjk_style_index(styles_root, original_style, cjk_font_id, cjk_style_cache)))
                        styles_changed = True
                    storage = unit.location.node_ids[0]
                    if storage == "inline":
                        node = cell.find("{*}is/{*}t")
                        if node is None:
                            raise XlsxAdapterError(f"inline string changed for {unit.id}")
                        node.text = text
                        if text[0].isspace() or text[-1].isspace():
                            node.set(_XML_SPACE, "preserve")
                    else:
                        if sst_root is None or not storage.startswith("sst:"):
                            raise XlsxAdapterError(f"shared string changed for {unit.id}")
                        index = int(storage.removeprefix("sst:"))
                        clone = copy.deepcopy(strings[index])
                        # Phonetic metadata is tied to the original text.  A
                        # translated string must not retain stale ruby data,
                        # which makes Excel offer to repair the workbook.
                        for child in list(clone):
                            if child.tag.split("}")[-1] == "phoneticPr":
                                clone.remove(child)
                        node = clone.find("{*}t")
                        if node is None:
                            raise XlsxAdapterError(f"shared string changed for {unit.id}")
                        node.text = text
                        if text[0].isspace() or text[-1].isspace():
                            node.set(_XML_SPACE, "preserve")
                        sst_root.append(clone)
                        value = cell.find("{*}v")
                        if value is None:
                            raise XlsxAdapterError(f"shared string cell changed for {unit.id}")
                        value.text = str(len(strings))
                        strings.append(clone)
                        shared_changed = True
                rewritten[part] = _serialize_xml(root)
            if shared_changed:
                sst_root.set("uniqueCount", str(len(strings)))
                rewritten[_SST_PART] = _serialize_xml(sst_root)
            if styles_changed and styles_root is not None:
                rewritten[_STYLES_PART] = _serialize_xml(styles_root)
    except (zipfile.BadZipFile, ET.ParseError, KeyError, IndexError, ValueError) as exc:
        raise XlsxAdapterError("unable to safely rewrite XLSX") from exc

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False) as handle:
            temporary_name = handle.name
        with zipfile.ZipFile(_BytesReader(raw)) as source_zip, zipfile.ZipFile(temporary_name, "w") as output_zip:
            output_zip.comment = source_zip.comment
            for info in source_zip.infolist():
                output_zip.writestr(info, rewritten.get(info.filename, source_zip.read(info)))
        validate_xlsx_output(source, Path(temporary_name), rewritten_parts=set(rewritten))
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def validate_xlsx_output(
    source_path: str | Path,
    output_path: str | Path,
    *,
    rewritten_parts: set[str] | None = None,
) -> None:
    """Verify package membership, formula content, and non-target parts before delivery."""
    source, output = Path(source_path), Path(output_path)
    try:
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(output) as translated:
            original_names = [item.filename for item in original.infolist()]
            translated_names = [item.filename for item in translated.infolist()]
            if original_names != translated_names:
                raise XlsxAdapterError("XLSX output changed package members")
            changed = rewritten_parts or set()
            for name in original_names:
                if name not in changed and original.read(name) != translated.read(name):
                    raise XlsxAdapterError(f"XLSX output changed non-target part: {name}")
            if _formula_map(original) != _formula_map(translated):
                raise XlsxAdapterError("XLSX output changed formulas")
            _sheet_parts(translated)
            _shared_strings(translated)
            for _, part, _ in _sheet_parts(translated):
                ET.fromstring(translated.read(part))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise XlsxAdapterError("XLSX output package validation failed") from exc


def _formula_map(package: zipfile.ZipFile) -> dict[tuple[str, str], str]:
    formulas: dict[tuple[str, str], str] = {}
    for _, part, _ in _sheet_parts(package):
        root = ET.fromstring(package.read(part))
        for cell in root.findall(".//{*}sheetData/{*}row/{*}c"):
            formula = cell.find("{*}f")
            if formula is not None:
                reference = cell.get("r")
                if not reference:
                    raise XlsxAdapterError(f"XLSX formula cell is missing its reference in {part}")
                formulas[(part, reference)] = formula.text or ""
    return formulas


class _BytesReader:
    def __init__(self, value: bytes) -> None:
        import io

        self._buffer = io.BytesIO(value)

    def __getattr__(self, name: str):
        return getattr(self._buffer, name)
