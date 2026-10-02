"""Decide whether a document is Chinese or English from a sample of its text.

Only the zh <-> en pair is supported, so the target is always the other one.
A Chinese character carries about as much as 1.6 English words, so the share
compares CJK characters with weighted English words; between the two
thresholds (a bilingual drawing, a scanned PDF with no text) the caller has to
ask the user.
"""

from __future__ import annotations

import re
from pathlib import Path

_CJK_RE = re.compile(r"[㐀-鿿豈-﫿]")
_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_WORDS_PER_CJK = 1.6
_ZH_SHARE = 0.65
_EN_SHARE = 0.35
_MIN_SIGNAL = 20
_SAMPLE_CHARS = 60_000


def detect_text_language(text: str) -> str | None:
    """"zh", "en", or None when the sample is too small or too mixed."""
    cjk = len(_CJK_RE.findall(text))
    words = len(_WORD_RE.findall(text))
    weighted = cjk + _WORDS_PER_CJK * words
    if weighted < _MIN_SIGNAL:
        return None
    share = cjk / weighted
    if share >= _ZH_SHARE:
        return "zh"
    if share <= _EN_SHARE:
        return "en"
    return None


def other_language(language: str) -> str:
    return "en" if language == "zh" else "zh"


def sample_document_text(path: str | Path) -> str:
    """Up to ~60k characters of the document's own text (not DWG: that needs AutoCAD)."""
    source = Path(path)
    suffix = source.suffix.casefold()
    parts: list[str] = []
    size = 0

    def add(text: str) -> bool:
        nonlocal size
        if text:
            parts.append(text)
            size += len(text)
        return size >= _SAMPLE_CHARS

    if suffix in {".md", ".markdown", ".txt"}:
        add(source.read_text(encoding="utf-8-sig", errors="replace")[:_SAMPLE_CHARS])
    elif suffix == ".pdf":
        import fitz

        with fitz.open(source) as document:
            # Spread over the document: a cover or annex alone can mislead.
            count = len(document)
            pages = sorted({int(i * count / 30) for i in range(30)}) if count > 30 else range(count)
            for index in pages:
                if add(document[index].get_text()):
                    break
    elif suffix == ".docx":
        from docx import Document

        document = Document(str(source))
        for paragraph in document.paragraphs:
            if add(paragraph.text):
                break
        for table in document.tables:
            if size >= _SAMPLE_CHARS:
                break
            for row in table.rows:
                if add(" ".join(cell.text for cell in row.cells)):
                    break
    elif suffix == ".pptx":
        from pptx import Presentation

        for slide in Presentation(str(source)).slides:
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and add(shape.text_frame.text):
                    break
            if size >= _SAMPLE_CHARS:
                break
    elif suffix == ".xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(source, read_only=True, data_only=True)
        try:
            for sheet in workbook.worksheets:
                for row in sheet.iter_rows(values_only=True):
                    if add(" ".join(str(value) for value in row if isinstance(value, str))):
                        break
                if size >= _SAMPLE_CHARS:
                    break
        finally:
            workbook.close()
    return "\n".join(parts)


def detect_document_language(path: str | Path) -> str | None:
    return detect_text_language(sample_document_text(path))
