"""Validated, deterministic user glossary loading and lookup."""

from __future__ import annotations

import csv
import hashlib
import json
import posixpath
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree


class GlossaryError(ValueError):
    """Raised when a user glossary cannot be loaded or validated."""


@dataclass(frozen=True, slots=True)
class GlossaryEntry:
    """One literal source-term to target-term mapping."""

    source: str
    target: str


@dataclass(frozen=True, slots=True)
class Glossary:
    """An immutable glossary with a stable content fingerprint."""

    entries: tuple[GlossaryEntry, ...]
    version: str

    @classmethod
    def load(cls, path: str | Path) -> "Glossary":
        """Load and validate a CSV or XLSX glossary from *path*."""
        glossary_path = Path(path)
        suffix = glossary_path.suffix.lower()
        if suffix == ".csv":
            headers, rows = _read_csv(glossary_path)
        elif suffix == ".xlsx":
            headers, rows = _read_xlsx(glossary_path)
        else:
            raise GlossaryError("glossary file must have a .csv or .xlsx suffix")
        return cls._from_rows(headers, rows)

    @classmethod
    def _from_rows(cls, headers: list[str], rows: list[list[str]]) -> "Glossary":
        columns: dict[str, int] = {}
        for index, header in enumerate(headers):
            normalized = header.strip().casefold()
            if normalized in {"source", "target"}:
                if normalized in columns:
                    raise GlossaryError(f"duplicate {normalized!r} header")
                columns[normalized] = index
        if "source" not in columns or "target" not in columns:
            raise GlossaryError("glossary must contain source and target headers")

        mappings: dict[str, str] = {}
        source_index = columns["source"]
        target_index = columns["target"]
        for row_number, row in enumerate(rows, start=2):
            source = _cell_at(row, source_index).strip()
            target = _cell_at(row, target_index).strip()
            if not source or not target:
                raise GlossaryError(f"row {row_number} has an empty source or target")
            previous = mappings.get(source)
            if previous is not None and previous != target:
                raise GlossaryError(f"source {source!r} has conflicting targets")
            mappings[source] = target

        entries = tuple(
            GlossaryEntry(source=source, target=target)
            for source, target in sorted(mappings.items())
        )
        fingerprint_data = [[entry.source, entry.target] for entry in entries]
        version = hashlib.sha256(
            json.dumps(fingerprint_data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return cls(entries=entries, version=version)

    def entries_for(self, source_text: str) -> tuple[GlossaryEntry, ...]:
        """Return matched terms, tolerating ordinary English punctuation/number variants.

        Longer terms are matched first and the text they cover is set aside:
        "Sr. No." (序号) must not also demand the shorter "No." (编号), just as
        污水管 inside 污水管道 is not a second requirement.
        """
        remaining = _normalize_term_text(source_text)
        candidates = sorted(self.entries, key=lambda entry: (-len(entry.source), entry.source))
        selected: list[GlossaryEntry] = []
        for entry in candidates:
            normalized_source = _normalize_term_text(entry.source)
            if not normalized_source:
                continue
            # A multi-word term matches in any case ("Ultimate disposal
            # station" in running text); a short one such as "No." does not,
            # or every sentence ending in "no." would demand "编号".
            flags = re.IGNORECASE if case_insensitive_term(entry.source) else 0
            plural = "" if normalized_source.endswith("s") else "s?"
            pattern = re.compile(re.escape(normalized_source) + plural, flags)
            # A bare abbreviation ("No.") is a term only as a whole label (a
            # column header); inside text, "Schedule No. 2" is 2号附表 and
            # "Item No. 4.5" 第4.5项, not 编号.
            if len(normalized_source) <= 3 and not _contains_cjk(normalized_source):
                if remaining.strip().casefold() == normalized_source.casefold():
                    selected.append(entry)
                continue
            if pattern.search(remaining):
                selected.append(entry)
                remaining = pattern.sub("\x00", remaining)
        return tuple(selected)


def case_insensitive_term(source: str) -> bool:
    return not _contains_cjk(source) and len(source.split()) >= 2


def _normalize_term_text(value: str) -> str:
    """Normalize only matching syntax; never alter glossary output text."""
    value = value.replace("‐", "-").replace("‑", "-").replace("–", "-").replace("—", "-")
    value = re.sub(r"[-\s]+", " ", value)
    return value.strip()


def _contains_cjk(value: str) -> bool:
    return bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", value))


def load_glossary(path: str | Path) -> Glossary:
    """Load a glossary; convenience wrapper around :meth:`Glossary.load`."""
    return Glossary.load(path)


def _cell_at(row: list[str], index: int) -> str:
    return row[index] if index < len(row) else ""


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, strict=True)
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as error:
        raise GlossaryError("unable to read CSV glossary") from error
    if not rows:
        raise GlossaryError("glossary is missing a header row")
    return rows[0], rows[1:]


def _read_xlsx(path: Path) -> tuple[list[str], list[list[str]]]:
    try:
        with zipfile.ZipFile(path) as archive:
            shared_strings = _shared_strings(archive)
            worksheet_path = _first_worksheet_path(archive)
            root = ElementTree.fromstring(archive.read(worksheet_path))
    except (OSError, zipfile.BadZipFile, KeyError, ElementTree.ParseError, ValueError) as error:
        raise GlossaryError("unable to read XLSX glossary") from error

    worksheet_rows: list[list[str]] = []
    for row in root.findall(".//{*}sheetData/{*}row"):
        values: dict[int, str] = {}
        for cell in row.findall("{*}c"):
            reference = cell.get("r")
            if reference is None:
                raise GlossaryError("XLSX cell is missing its reference")
            column = _column_index(reference)
            values[column] = _xlsx_cell_value(cell, shared_strings)
        width = max(values, default=-1) + 1
        worksheet_rows.append([values.get(index, "") for index in range(width)])
    if not worksheet_rows:
        raise GlossaryError("glossary is missing a header row")
    return worksheet_rows[0], worksheet_rows[1:]


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    try:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    except ElementTree.ParseError as error:
        raise GlossaryError("XLSX shared strings are malformed") from error
    return ["".join(text.text or "" for text in item.findall(".//{*}t")) for item in root.findall("{*}si")]


def _first_worksheet_path(archive: zipfile.ZipFile) -> str:
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    sheet = workbook.find(".//{*}sheets/{*}sheet")
    if sheet is None:
        raise GlossaryError("XLSX workbook has no worksheet")
    relationship_id = sheet.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    if not relationship_id:
        raise GlossaryError("XLSX worksheet relationship is missing")
    relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    target = next(
        (item.get("Target") for item in relationships.findall("{*}Relationship") if item.get("Id") == relationship_id),
        None,
    )
    if not target:
        raise GlossaryError("XLSX worksheet relationship cannot be resolved")
    if target.startswith("/"):
        return posixpath.normpath(target.lstrip("/"))
    return posixpath.normpath(posixpath.join("xl", target))


def _xlsx_cell_value(cell: ElementTree.Element, shared_strings: list[str]) -> str:
    cell_type = cell.get("t")
    if cell_type == "inlineStr":
        return "".join(item.text or "" for item in cell.findall(".//{*}is//{*}t"))
    value = cell.findtext("{*}v", default="")
    if cell_type == "s":
        try:
            return shared_strings[int(value)]
        except (IndexError, ValueError) as error:
            raise GlossaryError("XLSX shared string reference is invalid") from error
    return value


def _column_index(reference: str) -> int:
    letters = "".join(character for character in reference if character.isalpha())
    if not letters:
        raise GlossaryError("XLSX cell reference is invalid")
    index = 0
    for letter in letters.upper():
        if not "A" <= letter <= "Z":
            raise GlossaryError("XLSX cell reference is invalid")
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1
