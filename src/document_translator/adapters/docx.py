"""Conservative OOXML text-node extraction and rewrite for DOCX packages.

This first adapter slice deliberately handles only ``word/document.xml``.  It
never rebuilds paragraphs or runs: supplied translations are assigned directly
to the existing eligible ``w:t`` elements in a newly-created package.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import zipfile
from pathlib import Path
import re
from typing import Mapping, Sequence
from xml.etree import ElementTree as ET

from document_translator.core import (
    DocumentFormat,
    DocumentLocation,
    TranslationResult,
    TranslationUnit,
    generate_unit_id,
    sha256_text,
    validate_result_for_unit,
)
from document_translator.font_policy import CJK_FONT, LATIN_FONT, contains_cjk
from document_translator.translation_rules import rule_protected_tokens

_DOCUMENT_PART = "word/document.xml"
_PART_ROOTS = {
    _DOCUMENT_PART: "document",
    "word/footnotes.xml": "footnotes",
    "word/endnotes.xml": "endnotes",
}
_HEADER_FOOTER_PART_RE = re.compile(r"word/(header|footer)\d+\.xml$")
_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_TEXT_TAG = f"{{{_WORD_NAMESPACE}}}t"
_DELETED_TAG = f"{{{_WORD_NAMESPACE}}}del"
_RUN_TAG = f"{{{_WORD_NAMESPACE}}}r"
_RUN_PROPERTIES_TAG = f"{{{_WORD_NAMESPACE}}}rPr"
_FONT_TAG = f"{{{_WORD_NAMESPACE}}}rFonts"
_INSTRUCTION_TAG = f"{{{_WORD_NAMESPACE}}}instrText"
_PARAGRAPH_TAG = f"{{{_WORD_NAMESPACE}}}p"
_BREAK_TAG = f"{{{_WORD_NAMESPACE}}}br"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_XMLNS_DECLARATION_RE = re.compile(br"\s(xmlns(?::[A-Za-z_][\w.-]*)?=\"[^\"]+\")")


class DocxAdapterError(ValueError):
    """Raised when a DOCX package or its requested rewrite is unsafe."""


def _package_bytes(path: Path) -> bytes:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise DocxAdapterError(f"cannot read DOCX source: {path}") from exc
    if not zipfile.is_zipfile(path):
        raise DocxAdapterError("source is not a DOCX ZIP package")
    return raw


def _text_parts(raw: bytes) -> list[tuple[str, bytes]]:
    try:
        with zipfile.ZipFile(io := _BytesReader(raw)) as package:
            entries = package.infolist()
            document_entries = [info for info in entries if info.filename == _DOCUMENT_PART]
            if len(document_entries) != 1:
                raise DocxAdapterError("DOCX package must contain exactly one word/document.xml")
            selected = [
                info for info in entries
                if info.filename in _PART_ROOTS or _HEADER_FOOTER_PART_RE.fullmatch(info.filename)
            ]
            if len({info.filename for info in selected}) != len(selected):
                raise DocxAdapterError("DOCX package has duplicate translatable XML parts")
            return [(info.filename, package.read(info)) for info in selected]
    except zipfile.BadZipFile as exc:
        raise DocxAdapterError("source is not a readable DOCX ZIP package") from exc


class _BytesReader:
    """Small seekable bytes wrapper, avoiding a temporary unpacked document."""

    def __init__(self, value: bytes) -> None:
        import io

        self._buffer = io.BytesIO(value)

    def __getattr__(self, name: str):
        return getattr(self._buffer, name)


def _eligible_text_nodes(root: ET.Element) -> list[ET.Element]:
    nodes: list[ET.Element] = []

    def visit(element: ET.Element, in_deleted_revision: bool) -> None:
        deleted = in_deleted_revision or element.tag == _DELETED_TAG
        if element.tag == _TEXT_TAG and not deleted and (element.text or "").strip():
            nodes.append(element)
        for child in element:
            visit(child, deleted)

    visit(root, False)
    return nodes


def _eligible_text_groups(root: ET.Element) -> list[tuple[ET.Element, ...]]:
    """Merge only adjacent plain runs with byte-identical run properties."""
    nodes = _eligible_text_nodes(root)
    parents = {child: parent for parent in root.iter() for child in parent}
    groups: list[tuple[ET.Element, ...]] = []

    def can_join(left: ET.Element, right: ET.Element) -> bool:
        left_run, right_run = parents.get(left), parents.get(right)
        if left_run is None or right_run is None or left_run.tag != _RUN_TAG or right_run.tag != _RUN_TAG:
            return False
        parent = parents.get(left_run)
        if parent is None or parents.get(right_run) is not parent:
            return False
        children = list(parent)
        if children.index(right_run) != children.index(left_run) + 1:
            return False
        if any(child.tag == _INSTRUCTION_TAG for child in left_run.iter()) or any(child.tag == _INSTRUCTION_TAG for child in right_run.iter()):
            return False
        if [child for child in left_run if child.tag == _TEXT_TAG] != [left]:
            return False
        if [child for child in right_run if child.tag == _TEXT_TAG] != [right]:
            return False
        def signature(run: ET.Element) -> bytes:
            properties = run.find(_RUN_PROPERTIES_TAG)
            return ET.tostring(properties, encoding="utf-8") if properties is not None else b""
        return signature(left_run) == signature(right_run)

    for node in nodes:
        if groups and can_join(groups[-1][-1], node):
            groups[-1] = (*groups[-1], node)
        else:
            groups.append((node,))
    return groups


def _eligible_paragraph_groups(root: ET.Element) -> list[tuple[ET.Element, ...]]:
    """Return each visible paragraph as one translation unit.

    This mode prioritizes coherent translation over preserving separate run
    fragments. The rewritten translation is put in the paragraph's first
    eligible text node; the other text nodes are emptied in place.
    """
    groups: list[tuple[ET.Element, ...]] = []
    for paragraph in root.iter(_PARAGRAPH_TAG):
        nodes = tuple(_eligible_text_nodes(paragraph))
        if nodes:
            groups.append(nodes)
    return groups


def _expected_root(part: str) -> str:
    if part in _PART_ROOTS:
        return _PART_ROOTS[part]
    match = _HEADER_FOOTER_PART_RE.fullmatch(part)
    if match:
        return {"header": "hdr", "footer": "ftr"}[match.group(1)]
    raise DocxAdapterError(f"unsupported DOCX text part: {part}")


def _parse_part(part: str, xml: bytes) -> tuple[ET.Element, list[tuple[ET.Element, ...]]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DocxAdapterError(f"{part} is not valid XML") from exc
    if root.tag != f"{{{_WORD_NAMESPACE}}}{_expected_root(part)}":
        raise DocxAdapterError(f"{part} has an unexpected root element")
    return root, _eligible_text_groups(root)


def _serialize_part(root: ET.Element, original_xml: bytes) -> bytes:
    """Keep root namespace declarations required by mc:Ignorable for Word."""
    serialized = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    root_start = original_xml.find(b"<", original_xml.find(b"?>") + 2)
    original_root = original_xml[root_start:original_xml.find(b">", root_start) + 1]
    declarations = _XMLNS_DECLARATION_RE.findall(original_root)
    missing = [declaration for declaration in declarations if declaration not in serialized]
    if not missing:
        return serialized
    root_end = serialized.find(b">", serialized.find(b"?>") + 2)
    return serialized[:root_end] + b" " + b" ".join(missing) + serialized[root_end:]


def _remove_redundant_soft_breaks(root: ET.Element) -> None:
    """Remove a soft break only when its paragraph has no later visible text."""
    parents = {child: parent for parent in root.iter() for child in parent}
    for paragraph in root.iter(_PARAGRAPH_TAG):
        descendants = list(paragraph.iter())
        for node in list(descendants):
            if node.tag != _BREAK_TAG:
                continue
            following = descendants[descendants.index(node) + 1:]
            if any((item.text or "").strip() for item in following if item.tag == _TEXT_TAG):
                continue
            parents[node].remove(node)


def _apply_cjk_font_policy(root: ET.Element, group: Sequence[ET.Element], text: str) -> None:
    """Set separate Latin/East-Asian fonts without rebuilding DOCX runs."""
    if not contains_cjk(text):
        return
    parents = {child: parent for parent in root.iter() for child in parent}
    runs: set[ET.Element] = set()
    for node in group:
        current = parents.get(node)
        while current is not None and current.tag != _RUN_TAG:
            current = parents.get(current)
        if current is not None:
            runs.add(current)
    for run in runs:
        rpr = run.find(_RUN_PROPERTIES_TAG)
        if rpr is None:
            rpr = ET.Element(_RUN_PROPERTIES_TAG)
            run.insert(0, rpr)
        fonts = rpr.find(_FONT_TAG)
        if fonts is None:
            fonts = ET.Element(_FONT_TAG)
            rpr.insert(0, fonts)
        fonts.set(f"{{{_WORD_NAMESPACE}}}ascii", LATIN_FONT)
        fonts.set(f"{{{_WORD_NAMESPACE}}}hAnsi", LATIN_FONT)
        fonts.set(f"{{{_WORD_NAMESPACE}}}eastAsia", CJK_FONT)
        fonts.set(f"{{{_WORD_NAMESPACE}}}cs", LATIN_FONT)


def extract_translation_units(
    path: str | Path, *, source_language: str = "auto", target_language: str = "en",
) -> list[TranslationUnit]:
    """Extract stable units from ordinary non-deleted ``w:t`` nodes only."""
    source = Path(path)
    raw = _package_bytes(source)
    document_hash = hashlib.sha256(raw).hexdigest()
    units: list[TranslationUnit] = []
    for part, xml in _text_parts(raw):
        _, groups = _parse_part(part, xml)
        node_index = 0
        for group in groups:
            indexes = list(range(node_index, node_index + len(group)))
            location = DocumentLocation(
                part=part,
                object_id=f"w:t:{indexes[0]}",
                node_ids=[f"w:t:{index}" for index in indexes],
            )
            data = dict(
                document_hash=document_hash,
                format=DocumentFormat.DOCX,
                location=location,
                source_language=source_language,
                target_language=target_language,
                source_text="".join(node.text or "" for node in group),
                protected_tokens=rule_protected_tokens("".join(node.text or "" for node in group)),
                style_signature="",
                context_before="",
                context_after="",
            )
            data["id"] = generate_unit_id(**data)
            units.append(TranslationUnit.model_validate(data))
            node_index += len(group)
    return units


# The explicit alias keeps this format-specific operation discoverable while
# matching the common adapter convention used by Markdown.
extract_docx_units = extract_translation_units


def extract_paragraph_translation_units(
    path: str | Path, *, source_language: str = "auto", target_language: str = "en",
) -> list[TranslationUnit]:
    """Extract one translation unit per visible paragraph in supported parts."""
    source = Path(path)
    raw = _package_bytes(source)
    document_hash = hashlib.sha256(raw).hexdigest()
    units: list[TranslationUnit] = []
    for part, xml in _text_parts(raw):
        root, _ = _parse_part(part, xml)
        groups = _eligible_paragraph_groups(root)
        node_indexes = {node: index for index, node in enumerate(_eligible_text_nodes(root))}
        for paragraph_index, group in enumerate(groups):
            data = dict(
                document_hash=document_hash,
                format=DocumentFormat.DOCX,
                location=DocumentLocation(
                    part=part,
                    object_id=f"w:p:{paragraph_index}",
                    node_ids=[f"w:t:{node_indexes[node]}" for node in group],
                ),
                source_language=source_language,
                target_language=target_language,
                source_text="".join(node.text or "" for node in group),
                protected_tokens=rule_protected_tokens("".join(node.text or "" for node in group)),
                style_signature="",
                context_before="",
                context_after="",
            )
            data["id"] = generate_unit_id(**data)
            units.append(TranslationUnit.model_validate(data))
    return units


_INTEGRITY_ERRORS = {"UNIT_ID_MISMATCH", "SOURCE_HASH_MISMATCH", "RESULT_HASH_MISMATCH", "EMPTY_TRANSLATION"}


def _translation_text(unit: TranslationUnit, candidate: str | TranslationResult) -> str:
    if isinstance(candidate, TranslationResult):
        # Only a result that does not belong to this unit stops the write;
        # content findings were already retried and reported by the service.
        errors = [error for error in validate_result_for_unit(unit, candidate) if error.split(":")[0] in _INTEGRITY_ERRORS]
        if errors:
            raise DocxAdapterError(f"invalid result for {unit.id}: {', '.join(errors)}")
        value = candidate.translation
    elif isinstance(candidate, str):
        value = candidate
    else:
        raise DocxAdapterError(f"invalid translation value for {unit.id}")
    if not value.strip():
        raise DocxAdapterError(f"empty translation for {unit.id}")
    if any(ord(char) < 0x20 and char not in "\t\n\r" for char in value):
        raise DocxAdapterError(f"translation contains XML-illegal control characters for {unit.id}")
    return value


def _node_index(unit: TranslationUnit) -> int:
    if unit.location.part not in _PART_ROOTS and not _HEADER_FOOTER_PART_RE.fullmatch(unit.location.part):
        raise DocxAdapterError(f"invalid DOCX text part for {unit.id}")
    if unit.format is not DocumentFormat.DOCX:
        raise DocxAdapterError(f"invalid DOCX unit location for {unit.id}")
    prefix = "w:t:"
    object_id = unit.location.object_id or ""
    if not object_id.startswith(prefix) or not object_id[len(prefix):].isdigit():
        raise DocxAdapterError(f"invalid DOCX text-node identifier for {unit.id}")
    identity = unit.model_dump(exclude={"id", "status"})
    if generate_unit_id(**identity) != unit.id:
        raise DocxAdapterError(f"invalid DOCX unit ID for {unit.id}")
    return int(object_id[len(prefix):])


def _paragraph_index(unit: TranslationUnit) -> int:
    prefix = "w:p:"
    object_id = unit.location.object_id or ""
    if unit.format is not DocumentFormat.DOCX or not object_id.startswith(prefix) or not object_id[len(prefix):].isdigit():
        raise DocxAdapterError(f"invalid DOCX paragraph identifier for {unit.id}")
    identity = unit.model_dump(exclude={"id", "status"})
    if generate_unit_id(**identity) != unit.id:
        raise DocxAdapterError(f"invalid DOCX unit ID for {unit.id}")
    return int(object_id[len(prefix):])


def rewrite_docx(
    source_path: str | Path,
    destination_path: str | Path,
    units: Sequence[TranslationUnit],
    translations: Mapping[str, str | TranslationResult],
) -> None:
    """Write a translated DOCX copy after validating all source-node mappings."""
    source = Path(source_path)
    destination = Path(destination_path)
    if source.resolve() == destination.resolve():
        raise DocxAdapterError("source and destination must differ")
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise DocxAdapterError(f"destination directory does not exist: {destination.parent}")

    raw = _package_bytes(source)
    document_hash = hashlib.sha256(raw).hexdigest()
    source_parts = dict(_text_parts(raw))
    parsed_parts = {part: _parse_part(part, xml) for part, xml in source_parts.items()}
    ordered_units = sorted(units, key=lambda unit: (unit.location.part, _node_index(unit)))
    expected_unit_count = sum(len(groups) for _, groups in parsed_parts.values())
    if len(ordered_units) != expected_unit_count:
        raise DocxAdapterError("units do not match the current source DOCX text-node mapping")
    units_by_part: dict[str, list[TranslationUnit]] = {}
    for unit in ordered_units:
        units_by_part.setdefault(unit.location.part, []).append(unit)
    expected_parts_with_units = {part for part, (_, groups) in parsed_parts.items() if groups}
    if set(units_by_part) != expected_parts_with_units:
        raise DocxAdapterError("units do not match the current source DOCX text-node mapping")
    for part, (_, groups) in parsed_parts.items():
        part_units = units_by_part.get(part, [])
        expected_indexes = []
        next_index = 0
        for group in groups:
            expected_indexes.append(list(range(next_index, next_index + len(group))))
            next_index += len(group)
        if [
            [int(node_id.removeprefix("w:t:")) for node_id in unit.location.node_ids]
            for unit in part_units
        ] != expected_indexes:
            raise DocxAdapterError("units do not match the current source DOCX text-node mapping")
        for unit, group in zip(part_units, groups, strict=True):
            if unit.document_hash != document_hash or unit.source_text != "".join(node.text or "" for node in group):
                raise DocxAdapterError("units do not match the current source DOCX text-node mapping")
    expected_ids = {unit.id for unit in ordered_units}
    if len(expected_ids) != len(ordered_units):
        raise DocxAdapterError("duplicate DOCX translation unit IDs")
    supplied_ids = set(translations)
    if supplied_ids != expected_ids:
        missing = expected_ids - supplied_ids
        unknown = supplied_ids - expected_ids
        detail = "missing" if missing else "unknown"
        raise DocxAdapterError(f"translation mapping has {detail} unit IDs")

    rewritten_parts: dict[str, bytes] = {}
    for part, (root, groups) in parsed_parts.items():
        for group, unit in zip(groups, units_by_part.get(part, []), strict=True):
            value = _translation_text(unit, translations[unit.id])
            group[0].text = value
            if value[0].isspace() or value[-1].isspace():
                group[0].set(_XML_SPACE, "preserve")
            for node in group[1:]:
                node.text = ""
            _apply_cjk_font_policy(root, group, value)
        _remove_redundant_soft_breaks(root)
        rewritten_parts[part] = _serialize_part(root, source_parts[part])

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary_name = handle.name
        with zipfile.ZipFile(_BytesReader(raw)) as source_zip, zipfile.ZipFile(
            temporary_name, "w",
        ) as output_zip:
            output_zip.comment = source_zip.comment
            for info in source_zip.infolist():
                payload = rewritten_parts.get(info.filename, source_zip.read(info))
                output_zip.writestr(info, payload)
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def rewrite_docx_paragraphs(
    source_path: str | Path,
    destination_path: str | Path,
    units: Sequence[TranslationUnit],
    translations: Mapping[str, str | TranslationResult],
) -> None:
    """Write paragraph-priority DOCX translations into a new package only."""
    source = Path(source_path)
    destination = Path(destination_path)
    if source.resolve() == destination.resolve():
        raise DocxAdapterError("source and destination must differ")
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    if not destination.parent.is_dir():
        raise DocxAdapterError(f"destination directory does not exist: {destination.parent}")

    raw = _package_bytes(source)
    document_hash = hashlib.sha256(raw).hexdigest()
    source_parts = dict(_text_parts(raw))
    parsed_parts = {
        part: (root := _parse_part(part, xml)[0], _eligible_paragraph_groups(root))
        for part, xml in source_parts.items()
    }
    ordered_units = sorted(units, key=lambda unit: (unit.location.part, _paragraph_index(unit)))
    expected_count = sum(len(groups) for _, groups in parsed_parts.values())
    if len(ordered_units) != expected_count:
        raise DocxAdapterError("units do not match the current source DOCX paragraph mapping")
    units_by_part: dict[str, list[TranslationUnit]] = {}
    for unit in ordered_units:
        units_by_part.setdefault(unit.location.part, []).append(unit)
    expected_parts = {part for part, (_, groups) in parsed_parts.items() if groups}
    if set(units_by_part) != expected_parts:
        raise DocxAdapterError("units do not match the current source DOCX paragraph mapping")
    for part, (root, groups) in parsed_parts.items():
        part_units = units_by_part.get(part, [])
        node_indexes = {node: index for index, node in enumerate(_eligible_text_nodes(root))}
        expected_ids = [[f"w:t:{node_indexes[node]}" for node in group] for group in groups]
        if [unit.location.node_ids for unit in part_units] != expected_ids:
            raise DocxAdapterError("units do not match the current source DOCX paragraph mapping")
        for unit, group in zip(part_units, groups, strict=True):
            if unit.document_hash != document_hash or unit.source_text != "".join(node.text or "" for node in group):
                raise DocxAdapterError("units do not match the current source DOCX paragraph mapping")
    expected_unit_ids = {unit.id for unit in ordered_units}
    if len(expected_unit_ids) != len(ordered_units) or set(translations) != expected_unit_ids:
        raise DocxAdapterError("translation mapping does not match DOCX paragraph units")

    rewritten_parts: dict[str, bytes] = {}
    for part, (root, groups) in parsed_parts.items():
        for group, unit in zip(groups, units_by_part.get(part, []), strict=True):
            value = _translation_text(unit, translations[unit.id])
            group[0].text = value
            if value[0].isspace() or value[-1].isspace():
                group[0].set(_XML_SPACE, "preserve")
            for node in group[1:]:
                node.text = ""
            _apply_cjk_font_policy(root, group, value)
        _remove_redundant_soft_breaks(root)
        rewritten_parts[part] = _serialize_part(root, source_parts[part])

    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp", delete=False) as handle:
            temporary_name = handle.name
        with zipfile.ZipFile(_BytesReader(raw)) as source_zip, zipfile.ZipFile(temporary_name, "w") as output_zip:
            output_zip.comment = source_zip.comment
            for info in source_zip.infolist():
                output_zip.writestr(info, rewritten_parts.get(info.filename, source_zip.read(info)))
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
