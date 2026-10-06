"""Translate a PDF in place: keep every original page object, replace only its text.

The output starts as a byte copy of the source. Images, seals, ruled table
grids, decorations, headers and footers are never redrawn or re-created --
only the original text glyphs that are being translated are removed, and the
translation is written back in the same place, in the source's own colour,
alignment and (where it fits) size. When a translation does not fit the space
its source text occupied, the font size is stepped down until it does.

Paragraphs are rebuilt from the source PDF's own text lines rather than from
MinerU's layout model: on a real document MinerU split one article in half and
glued its second half onto the next article ("...经营业务。第三条..."), which no
later step can undo. Chinese official documents mark paragraph starts very
regularly (a structural label such as 第X条, a first-line indent, a line that
ends early), and those signals are read directly from the source geometry.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
import hashlib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Callable

from document_translator.core import (
    DocumentFormat,
    DocumentLocation,
    TranslationResult,
    TranslationUnit,
    generate_unit_id,
    sha256_text,
    validate_result_for_unit,
)
from document_translator.translation_rules import localize_chinese_dates, normalize_chinese_spacing, restore_list_markers, rule_protected_tokens

from .mineru_pdf import (
    TranslationBatchProvider,
    _remediate_warnings,
    _source_cell_alignment,
    _table_cell_font,
    _translate_batch_with_retry,
    _write_failed_report,
)
from .pdf_layout import (
    _alignment,
    _font_file,
    _rgb,
    _role_for_source,
    load_numbering_profile,
    normalize_document_reference_translation,
    normalize_numbered_translation,
)
from .pdf_pipeline import (
    PdfPreflight,
    PdfPreflightError,
    inspect_pdf,
    publish_candidate,
    repair_pdf_text_cmaps,
    validate_candidate,
    write_pdf_report,
)

_CJK_RE = re.compile(r"[\u3400-\u9fff\uf900-\ufaff]")
_CJK_END_RE = re.compile(r"[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]$")
_CJK_START_RE = re.compile(r"^[\u3000-\u303f\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]")
# A line that opens with a structural label always starts a new paragraph.
_LABEL_RE = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百零〇\d]+[章节条款部分]"
    r"|[一二三四五六七八九十]+[、．.]"
    r"|[（(][一二三四五六七八九十\d]+[）)]"
    r"|\d+(?:\.\d+)*[、．.](?!\d))"
)
_SYMBOL_FONT_RE = re.compile(r"ESRI|Marker|Symbol|Wingding|Webding|Dingbat", re.IGNORECASE)
_FONT_STEP = 0.5
# Only a runaway guard: sizes keep stepping down until the text fits.
_ABSOLUTE_MIN_SIZE = 1.0


@dataclass
class _Segment:
    text: str
    bbox: tuple[float, float, float, float]
    spans: list[dict[str, Any]]


@dataclass
class _VisualLine:
    segments: list[_Segment]
    # A symbol-font bullet sits just left of the line: a list item starts.
    bulleted: bool = False
    # The filled box (a diagram node, a slide panel) the line sits in.
    box: tuple[float, float, float, float] | None = None
    # A row of a table cell read line by line (_row_list_cells).
    in_cell: bool = False

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        boxes = [segment.bbox for segment in self.segments]
        return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))

    @property
    def spans(self) -> list[dict[str, Any]]:
        return [span for segment in self.segments for span in segment.spans]

    @property
    def size(self) -> float:
        # The size most of the line's characters have: a superscript ("18th")
        # is a span of its own, and counted as one of three spans it made
        # the line 10pt and split the 12pt paragraph around it.
        sizes = sorted(
            (float(span.get("size") or 0.0), max(1, len(str(span.get("text", "")).strip())))
            for span in self.spans
        )
        half, seen = sum(weight for _, weight in sizes) / 2, 0
        for value, weight in sizes:
            seen += weight
            if seen >= half:
                return value
        return sizes[-1][0] if sizes else 0.0

    @property
    def baseline(self) -> float:
        """Baseline of the line's main text (a superscript sits above it)."""
        main = max(self.spans, key=lambda span: len(str(span.get("text", "")).strip()))
        origin = main.get("origin")
        return float(origin[1]) if origin else float(self.bbox[3])

    @property
    def color(self) -> int:
        """The colour of most of the line's letters (a coloured bullet "•"
        opening a black line made the whole translation that colour)."""
        from collections import Counter

        counts: Counter[int] = Counter()
        for span in self.spans:
            counts[int(span.get("color") or 0)] += sum(1 for char in str(span.get("text", "")) if char.isalnum())
        if not any(counts.values()):
            return int(self.spans[0].get("color") or 0)
        return counts.most_common(1)[0][0]

    @property
    def font(self) -> str:
        return str(self.spans[0].get("font") or "")

    @property
    def text(self) -> str:
        parts: list[str] = []
        previous: _Segment | None = None
        for segment in sorted(self.segments, key=lambda s: s.bbox[0]):
            if previous is not None and segment.bbox[0] - previous.bbox[2] > 0.5 * self.size:
                parts.append(" ")
            parts.append(segment.text)
            previous = segment
        return "".join(parts).strip()


@dataclass
class _Paragraph:
    page_number: int
    lines: list[_VisualLine]
    centred: bool
    page_width: float = 595.0
    bounds: tuple[float, float] = (72.0, 523.0)
    left_anchored: bool = False
    # The CJK face for this page, chosen before anything is written on it.
    cjk_font: str | None = None
    # A contents entry's page number: the title is translated, the dot
    # leader and number are set again to end where they did.
    toc_page: str | None = None

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        boxes = [line.bbox for line in self.lines]
        return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))

    @property
    def size(self) -> float:
        return median(line.size for line in self.lines)

    @property
    def text(self) -> str:
        result = ""
        for line in self.lines:
            text = line.text
            # "Drawing No. LW-" / "TD-401": a line broken after a hyphen
            # continues the same word or code, with no space.
            hyphen_break = bool(re.search(r"[A-Za-z0-9]-$", result)) and text[:1].isalnum()
            if result and not hyphen_break and not (_CJK_END_RE.search(result) and _CJK_START_RE.search(text)):
                result += " "
            result += text
        return result.strip()


class InPlacePdfTranslationService:
    """Translate a text-layer PDF by editing only its text, never its page objects."""

    def __init__(
        self,
        provider: TranslationBatchProvider,
        *,
        cache: Any | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.provider = provider
        self.cache = cache
        self.progress = progress
        self.model = str(getattr(provider, "model", getattr(getattr(provider, "config", None), "model", "")))

    def translate_file(
        self,
        source_path: str | Path,
        destination_path: str | Path,
        *,
        source_language: str,
        target_language: str,
        style_profile: str | Path | None = None,
        report_path: str | Path | None = None,
        allow_cad_pdf: bool = False,
        allow_complex_pdf: bool = False,
        minimum_font_size: float = 6.0,
    ) -> tuple[Path, PdfPreflight, Path]:
        source, destination = Path(source_path).resolve(), Path(destination_path).resolve()
        if source == destination:
            raise ValueError("source and destination paths must differ")
        if destination.exists():
            raise FileExistsError("destination already exists; choose a new path")
        report_file = Path(report_path or destination.with_suffix(".pdf-translation.json")).resolve()
        preflight = inspect_pdf(source)
        if preflight.classification in {"D", "E", "F"}:
            raise PdfPreflightError(
                f"PDF class {preflight.classification} has no usable text layer for in-place translation: "
                + "; ".join(preflight.reasons)
            )
        if preflight.classification == "C" and not allow_cad_pdf:
            raise PdfPreflightError(
                "PDF class C requires --allow-cad-pdf after confirming that the source DWG is unavailable"
            )
        if preflight.classification == "B" and not allow_complex_pdf:
            raise PdfPreflightError(
                "PDF class B requires --allow-complex-pdf after completing the required visual review"
            )
        profile = load_numbering_profile(style_profile, target_language=target_language)
        run: dict[str, object] = {"engine": "inplace", "source_hash": preflight.source_hash}
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="document-translator-inplace-", dir=destination.parent, ignore_cleanup_errors=True) as workdir_text:
            candidate = Path(workdir_text) / "inplace-candidate.pdf"
            try:
                run.update(
                    _translate_in_place(
                        self.provider,
                        source,
                        candidate,
                        source_hash=preflight.source_hash,
                        source_language=source_language,
                        target_language=target_language,
                        profile=profile,
                        cache=self.cache,
                        progress=self.progress,
                    )
                )
                run["cmap_repairs"] = repair_pdf_text_cmaps(candidate)
                validation = validate_candidate(
                    source,
                    candidate,
                    preflight,
                    target_language=target_language,
                    layout_profile=profile,
                    minimum_font_size=minimum_font_size,
                )
                report = write_pdf_report(
                    report_file,
                    preflight=preflight,
                    validation=validation,
                    provider=self.provider.provider_name,
                    model=self.model,
                    run=run,
                )
                return publish_candidate(candidate, destination), preflight, report
            except Exception as error:
                _write_failed_report(
                    report_file,
                    preflight=preflight,
                    provider=self.provider.provider_name,
                    model=self.model,
                    run=run,
                    error=error,
                )
                raise


def _translate_in_place(
    provider: TranslationBatchProvider,
    source: Path,
    candidate: Path,
    *,
    source_hash: str,
    source_language: str,
    target_language: str,
    profile: object,
    cache: Any | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    import fitz

    from . import pdf_table

    # A page stored rotated (/Rotate 90) is rewritten, on a working copy, to
    # look the same with its text horizontal; it was left untranslated.
    with fitz.open(source) as original:
        if any(page.rotation for page in original):
            for page in original:
                if page.rotation:
                    page.remove_rotation()
            upright = candidate.with_name(candidate.stem + ".upright" + candidate.suffix)
            original.save(str(upright), garbage=1, deflate=True)
            source = upright

    try:
        tables = pdf_table.extract_pdf_tables(source)
    except pdf_table.PdfTableError:
        tables = []
    doc = fitz.open(source)
    highlights = _collect_highlights(doc)
    _CELL_SOURCE_FONTS.clear()
    for table in tables:
        page = doc[table.page_number - 1]
        for cell in table.cells:
            if cell.rect is not None and not cell.is_empty:
                _CELL_SOURCE_FONTS[cell.id] = _dominant_font(page, cell.rect)
    try:
        # A page stored rotated (/Rotate 90) has all its text rotated, which
        # the paragraph path leaves untouched; its tables stay too: written
        # unrotated, their cells came out as scattered vertical words.
        # A floor plan is told by its overlapping "cells": checked before a
        # cell holding others is dropped, or it is read as a table again.
        tables = [
            _without_spanning_cells(t) for t in tables
            if not _cuts_through_text(t) and not _is_drawing_frame(t, doc[t.page_number - 1]) and not doc[t.page_number - 1].rotation
        ]
        tables = [
            _split_around_images(
                doc[t.page_number - 1], t,
                nested=[o.rect for o in tables if o is not t and o.page_number == t.page_number],
            ) for t in tables
            if not _is_drawing_frame(t, doc[t.page_number - 1])
        ]
        # A cell holding a column of one-line rows (a Gantt chart's task
        # names, with no rule between the rows) is not one paragraph: its
        # lines go through the paragraph path, each where it stands.
        row_cells = {cell.id for t in tables for cell in _row_list_cells(doc[t.page_number - 1], t)}
        covered = [
            _Covered(cell.page_number, cell.rect)
            for t in tables for cell in t.cells if cell.rect is not None and cell.id not in row_cells
        ]
        bounds_by_size = _content_bounds(doc, tables)
        paragraphs: list[_Paragraph] = []
        skipped_rotated = 0
        for page_number, page in enumerate(doc, start=1):
            table_rects = [fitz.Rect(c.rect) for c in covered if c.page_number == page_number]
            lines, rotated = _visual_lines(page, table_rects)
            skipped_rotated += rotated
            row_rects = [fitz.Rect(c.rect) for t in tables for c in t.cells if c.id in row_cells and c.page_number == page_number]
            for line in lines:
                line.in_cell = any(rect.contains(fitz.Point((line.bbox[0] + line.bbox[2]) / 2, (line.bbox[1] + line.bbox[3]) / 2)) for rect in row_rects)
            page_bounds = _body_edges(lines, bounds_by_size[_page_key(page)])
            page_paragraphs = _segment(page_number, lines, page_bounds, _horizontal_rules(page))
            gutter = _gutter(lines, page_bounds)
            for paragraph in page_paragraphs:
                paragraph.page_width = float(page.rect.width)
                paragraph.bounds = page_bounds
                # A box's text is written within the box, centred there as it
                # was ("1. Collection System" in a diagram node).
                box = paragraph.lines[0].box
                if box is not None and all(line.box == box for line in paragraph.lines):
                    pad = 0.15 * paragraph.size
                    paragraph.bounds = (box[0] + pad, box[2] - pad)
                    middle = (box[0] + box[2]) / 2
                    paragraph.centred = all(abs((line.bbox[0] + line.bbox[2]) / 2 - middle) <= max(4.0, 0.5 * line.size) for line in paragraph.lines)
                # A column's paragraph is written within its column.
                elif gutter is not None and paragraph.bbox[2] <= gutter:
                    paragraph.bounds = (min(page_bounds[0], paragraph.bbox[0]), gutter - 0.5 * paragraph.size)
                elif gutter is not None and paragraph.bbox[0] >= gutter:
                    column_left = min(line.bbox[0] for line in lines if line.bbox[0] >= gutter)
                    paragraph.bounds = (column_left, page_bounds[1])
                # Shares its left edge with other text (a legend column, a
                # list): left-anchored even if it happens to reach the right.
                x0 = paragraph.bbox[0]
                paragraph.left_anchored = any(
                    other is not paragraph and not other.centred and abs(other.bbox[0] - x0) <= 1.5
                    for other in page_paragraphs
                )
                paragraphs.append(paragraph)
        page_heights = {number: float(page.rect.height) for number, page in enumerate(doc, start=1)}
    finally:
        doc.close()

    translatable = [p for p in paragraphs if _needs_translation(p.text, source_language)]
    # A paragraph broken by a page is translated as one: two half sentences
    # came back as two separate (and wrong) translations.
    across = _across_pages(translatable, page_heights)
    tails = set(across.values())
    for paragraph in translatable:
        entry = _TOC_ENTRY_RE.match(paragraph.text) if len(paragraph.lines) == 1 else None
        if entry:
            paragraph.toc_page = entry.group("page")
    paragraph_units = [
        _unit(
            _mark_highlights(
                (_TOC_ENTRY_RE.match(p.text).group("title") if p.toc_page else p.text)
                + (" " + translatable[across[index]].text if index in across else ""),
                highlights.get(p.page_number, ()), p.bbox,
            ),
            f"page:{p.page_number}", f"para:{index}", "paragraph", source_hash, source_language, target_language,
        )
        for index, p in enumerate(translatable)
    ]
    cell_units: list[TranslationUnit] = []
    cell_by_unit: dict[str, Any] = {}
    # A row list (read line by line) does not carry the next page's part of
    # its row: paired with one, that part was skipped as "continued" and
    # never translated -- a whole page left in English, with no warning.
    continued = {
        head: tail for head, tail in _continued_cells(tables).items()
        if head not in row_cells and tail.id not in row_cells
    }
    # Ids: a set of the cells themselves never matched "cell.id in", so each
    # continuation was also translated on its own and overwrote its share.
    continuations = {cell.id for cell in continued.values()}
    for table in tables:
        for cell in table.cells:
            if cell.is_empty or cell.id in continuations or cell.id in row_cells:
                continue
            text = cell.text + ("\n" + continued[cell.id].text if cell.id in continued else "")
            if not _needs_translation(text, source_language):
                continue
            # "Drawing No. LW-" / "TD-401" broken across cell lines is one code.
            cell_text = _structure_cell_text(re.sub(r"(?<=[A-Za-z0-9])-\n(?=[A-Za-z0-9])", "-", text))
            cell_text = _mark_highlights(cell_text, highlights.get(cell.page_number, ()), cell.rect)
            unit = _unit(cell_text, f"page:{cell.page_number}", cell.id, "table_cell", source_hash, source_language, target_language)
            cell_units.append(unit)
            cell_by_unit[unit.id] = cell

    # Every table text is translated by some path: as a cell, as part of
    # the row it continues, or line by line (a row list). One that none of
    # them took is reported, never silently left in the source language.
    missed = _untranslated_cells(tables, {cell.id for cell in cell_by_unit.values()}, row_cells, continued, source_language)

    requested = [unit for index, unit in enumerate(paragraph_units) if index not in tails]
    translations, warnings, translation_stats = _translate_all(provider, requested + cell_units, cache=cache, progress=progress)
    warnings.extend(
        {"object_id": cell.id, "errors": [f"UNTRANSLATED_CELL: no translation path took this cell: {cell.text[:60]!r}"]}
        for cell in missed
    )
    # Highlighted phrases come back marked: record where the translation of
    # each goes, and take the markers out of the text that is written.
    highlight_targets: list[tuple[int, Any, str, Any]] = []
    regions = {unit.id: (p.page_number, p.bbox) for unit, p in zip(paragraph_units, translatable)}
    regions.update({unit.id: (cell_by_unit[unit.id].page_number, cell_by_unit[unit.id].rect) for unit in cell_units})
    for unit_id, text in list(translations.items()):
        if text and "\u27e6" in text and unit_id in regions:
            page_number, region = regions[unit_id]
            for kind, (opening, closing) in _MARKS.items():
                color = next(
                    (h["color"] for h in highlights.get(page_number, ())
                     if h.get("kind") == kind and fitz.Rect(h["rect"]).intersects(fitz.Rect(region))),
                    None,
                )
                for phrase in re.findall(re.escape(opening) + r"(.*?)" + re.escape(closing), text, re.S):
                    if _strip_highlight_markers(phrase).strip():
                        highlight_targets.append((page_number, region, _strip_highlight_markers(phrase).strip(), (kind, color)))
        if text:
            # Chinese spacing applies to cached results too (written before
            # a rule such as "6000psi" existed).
            translations[unit_id] = normalize_chinese_spacing(_strip_highlight_markers(text), target_language)
    # The whole translation is set as one paragraph: from the first page's
    # place onward, continuing on the next page only if it does not fit.
    flow: dict[int, _Paragraph] = {}
    for head, tail in across.items():
        if translations.get(paragraph_units[head].id, "").strip():
            translations[paragraph_units[tail].id] = ""
            flow[id(translatable[head])] = translatable[tail]

    with fitz.open(source) as work:
        flowed = {id(tail) for tail in flow.values()}
        for paragraph, unit in zip(translatable, paragraph_units):
            if id(paragraph) in flowed:
                continue  # filled with whatever the head paragraph leaves over
            paragraph_translation = translations.get(unit.id, "").strip() or _strip_highlight_markers(unit.source_text)
            # Also for a cached result from before the provider restored them.
            paragraph_translation = restore_list_markers(unit.source_text, paragraph_translation)
            translations[unit.id] = _normalise_structure(
                paragraph, paragraph_translation, paragraph.bounds, profile, target_language
            )
        rendered = _render_paragraphs(work, paragraphs, translatable, paragraph_units, translations, covered, flow)
        staged = candidate.with_name(candidate.stem + ".paragraphs" + candidate.suffix)
        work.save(str(staged), garbage=1, deflate=True)

    cell_translations: dict[str, str] = {}
    cleared_tails: list[Any] = []
    joined_rows: dict[str, tuple[Any, Any, str]] = {}
    for unit in cell_units:
        cell = cell_by_unit[unit.id]
        translation = restore_list_markers(unit.source_text, translations.get(unit.id, "").strip())
        if cell.id in continued and translation:
            # Split over the two pages once the first page's table size is
            # known (_render_tables): filled at the source size, the cell was
            # left half empty when the table was then set smaller.
            joined_rows[cell.id] = (cell, continued[cell.id], translation)
            cell_translations[cell.id] = translation
        else:
            cell_translations[cell.id] = _restore_item_breaks(cell.text, translation) if translation else cell.text
    table_report = _render_tables(
        source, staged, candidate, tables, cell_translations, source_language, target_language, warnings,
        joined_rows=joined_rows, cleared_tails=cleared_tails,
    )
    rewritten = [(p.page_number, p.bbox) for p in translatable] + [
        (cell.page_number, cell.rect) for table in tables for cell in table.cells
        if cell.rect is not None and not cell.is_empty and cell_translations.get(cell.id, cell.text) != cell.text
    ] + [(cell.page_number, cell.rect) for cell in cleared_tails]
    highlight_report = _move_highlights(candidate, highlights, rewritten, highlight_targets)
    _clear_cells(candidate, cleared_tails)
    try:
        staged.unlink(missing_ok=True)
        if source.name.endswith(".upright" + source.suffix):
            source.unlink(missing_ok=True)
    except OSError:
        # Windows can still hold the intermediate file a moment (a virus
        # scanner); it lives in a temporary folder that is removed anyway.
        pass
    return {
        "paragraph_count": len(paragraphs),
        "translated_paragraph_count": len(translatable),
        "rendered_paragraphs": rendered,
        "table_translation": table_report,
        "rotated_lines_left_untouched": skipped_rotated,
        "highlights": highlight_report,
        "translation_warning_count": len(warnings),
        "translation": translation_stats,
        **({"translation_warnings": warnings} if warnings else {}),
    }


# ---------------------------------------------------------------- structure


@dataclass
class _Covered:
    """Part of a page a table renders (one of its cells)."""

    page_number: int
    rect: tuple[float, float, float, float]


def _row_list_cells(page: Any, table: Any) -> list[Any]:
    """Cells whose text is four or more separate one-line rows.

    Rows set at least 1.5 times their font size apart are a list of items
    (task names in a Gantt chart), not a paragraph wrapped in a cell:
    reflowed as one, they overlapped forty rows of the chart. Four rows are
    enough: a price-adjustment table's unruled "Index Description" column
    ("Non adjustable" / "Foreign expert/ skilled labour" / "Stainless
    Steel" / "HDPE lining", beside codes A-D) came back as one sentence.
    """
    import fitz

    result = []
    for cell in table.cells:
        if cell.rect is None or cell.is_empty or len(cell.text.splitlines()) < 2:
            continue
        rows: list[tuple[float, float]] = []
        widths: list[float] = []
        colours: set[int] = set()
        for block in page.get_text("dict", clip=fitz.Rect(cell.rect)).get("blocks", ()):
            for line in block.get("lines", ()):
                spans = [span for span in line.get("spans", ()) if str(span.get("text", "")).strip()]
                if spans and not any(abs(line["bbox"][1] - top) <= 1.0 for top, _ in rows):
                    rows.append((float(line["bbox"][1]), float(spans[0].get("size") or 0.0)))
                    widths.append(float(line["bbox"][2] - line["bbox"][0]))
                    colours.add(int(max(spans, key=lambda span: len(str(span.get("text", "")).strip())).get("color") or 0))
        # Lines in different colours are different texts (a blue "PART II
        # -Employer Requirements" over a black "Section 6 - ..."): run
        # together, the heading lost its line and its colour.
        if len(rows) >= 2 and len(colours) > 1:
            result.append(cell)
            continue
        if len(rows) < 4:
            continue
        tops = sorted(top for top, _ in rows)
        steps = sorted(b - a for a, b in zip(tops, tops[1:]))
        size = sorted(size for _, size in rows)[len(rows) // 2]
        # A wrapped paragraph fills the cell's width (justified prose with
        # wide spacing); one-line rows mostly do not.
        filled = sum(1 for width in widths if width >= 0.85 * (cell.rect[2] - cell.rect[0]))
        if size > 0 and steps[len(steps) // 2] >= 1.5 * size and filled * 2 < len(widths):
            result.append(cell)
    return result


_HIGHLIGHT_OPEN, _HIGHLIGHT_CLOSE = "\u27e6H\u27e7", "\u27e6/H\u27e7"
_UNDERLINE_OPEN, _UNDERLINE_CLOSE = "\u27e6U\u27e7", "\u27e6/U\u27e7"
_MARKS = {"highlight": (_HIGHLIGHT_OPEN, _HIGHLIGHT_CLOSE), "underline": (_UNDERLINE_OPEN, _UNDERLINE_CLOSE)}


def _collect_highlights(doc: Any) -> dict[int, list[dict[str, Any]]]:
    """Highlight annotations and the words they cover, per page.

    A highlight is an annotation over the source's own words ("location for
    disposal" in a reply). Rewritten, the words moved and the yellow stayed
    behind over other text or blank space; the phrase is now carried
    through translation and highlighted again where its translation is.
    """
    import fitz

    result: dict[int, list[dict[str, Any]]] = {}
    for number, page in enumerate(doc, start=1):
        annots = [annot for annot in page.annots() or () if annot.type[1] == "Highlight"]
        if not annots:
            continue
        words = page.get_text("words")
        for annot in annots:
            vertices = annot.vertices or []
            quads = [fitz.Quad(vertices[i:i + 4]).rect for i in range(0, len(vertices) - 3, 4)] or [annot.rect]
            covered = [
                w[4] for w in words
                if any(q.contains(fitz.Point((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)) for q in quads)
            ]
            if covered:
                result.setdefault(number, []).append({
                    "rect": tuple(annot.rect), "text": " ".join(covered), "color": annot.colors.get("stroke"),
                    "kind": "highlight",
                })
    # Underlined words: a thin rule just under them, as long as they are.
    # Rewritten, the rule stayed under other words ("设计、制造" struck
    # through); the phrase is carried through translation like a highlight.
    for number, page in enumerate(doc, start=1):
        rules = [
            (fitz.Rect(d["rect"]), d.get("color") or d.get("fill"))
            for d in page.get_drawings()
            if d.get("rect") is not None and d["rect"].height <= 1.5 and d["rect"].width > 4
        ]
        if not rules:
            continue
        words = page.get_text("words")
        for rule, color in rules:
            above = [
                w for w in words
                if rule.y0 - 0.35 * (w[3] - w[1]) <= w[3] <= rule.y0 + 0.25 * (w[3] - w[1])
                and min(w[2], rule.x1) - max(w[0], rule.x0) > 0.5 * (w[2] - w[0])
            ]
            if not above:
                continue
            span = fitz.Rect(min(w[0] for w in above), 0, max(w[2] for w in above), 1)
            # a rule as long as the words over it (a table border runs on)
            if rule.width > span.width + 6 or rule.width < 0.6 * span.width:
                continue
            result.setdefault(number, []).append({
                "rect": (span.x0, min(w[1] for w in above), span.x1, rule.y1),
                "text": " ".join(w[4] for w in sorted(above, key=lambda w: w[0])),
                "color": tuple(color) if color else (0, 0, 0), "kind": "underline", "rule": tuple(rule),
            })
    return result


def _mark_highlights(text: str, highlights: Any, region: Any) -> str:
    """``text`` with each highlighted phrase inside it between markers."""
    import fitz

    if not highlights or region is None:
        return text
    area = fitz.Rect(region)
    for highlight in highlights:
        if not area.intersects(fitz.Rect(highlight["rect"])):
            continue
        words = highlight["text"].split()
        pattern = r"\s+".join(re.escape(word) for word in words)
        match = re.search(pattern, text) if words else None
        opening, closing = _MARKS[highlight.get("kind", "highlight")]
        if match and "\u27e6" not in text[max(0, match.start() - 4):match.end() + 5] and "\u27e6" not in match.group(0):
            text = text[:match.start()] + opening + match.group(0) + closing + text[match.end():]
    return text


def _strip_highlight_markers(text: str) -> str:
    for opening, closing in _MARKS.values():
        text = text.replace(opening, "").replace(closing, "")
    return text


def _move_highlights(
    candidate: Path,
    highlights: dict[int, list[dict[str, Any]]],
    rewritten: list[tuple[int, Any]],
    targets: list[tuple[int, Any, str, Any]],
) -> dict[str, int]:
    """Drop highlights left over rewritten text; highlight each translated
    phrase where it now is."""
    import fitz

    if not highlights:
        return {"removed": 0, "added": 0, "not_found": 0}
    removed = added = missing = 0
    with fitz.open(candidate) as doc:
        for number, page in enumerate(doc, start=1):
            if number not in highlights:
                continue
            areas = [fitz.Rect(region) for page_number, region in rewritten if page_number == number and region is not None]
            for annot in list(page.annots() or ()):
                if annot.type[1] == "Highlight" and any(annot.rect.intersects(area) for area in areas):
                    page.delete_annot(annot)
                    removed += 1
            old_rules = [
                fitz.Rect(h["rule"]) for h in highlights[number]
                if h.get("kind") == "underline" and any(fitz.Rect(h["rect"]).intersects(area) for area in areas)
            ]
            for rule in old_rules:
                page.add_redact_annot(rule + (-0.3, -0.3, 0.3, 0.3), fill=False)
            if old_rules:
                page.apply_redactions(images=0, graphics=1, text=1)
                removed += len(old_rules)
            for page_number, region, phrase, (kind, color) in targets:
                if page_number != number:
                    continue
                clip = fitz.Rect(region) + (-3, -3, 3, 40)
                quads = _find_phrase(page, phrase, clip)
                if not quads:
                    missing += 1
                    continue
                if kind == "underline":
                    for quad in quads:
                        box = fitz.Quad(quad).rect
                        y = box.y1 - 0.08 * box.height
                        page.draw_line((box.x0, y), (box.x1, y), color=color or (0, 0, 0), width=max(0.5, 0.06 * box.height))
                else:
                    annot = page.add_highlight_annot(quads)
                    if color:
                        annot.set_colors(stroke=color)
                    annot.update()
                added += 1
        doc.save(str(candidate), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)
    return {"removed": removed, "added": added, "not_found": missing}


def _clear_cells(candidate: Path, cells: list[Any]) -> None:
    """Remove the text of ``cells``, leaving their rules."""
    import fitz

    if not cells:
        return
    with fitz.open(candidate) as doc:
        for cell in cells:
            page = doc[cell.page_number - 1]
            page.add_redact_annot(fitz.Rect(cell.rect) + (1, 1, -1, -1), fill=False)
        for number in {cell.page_number for cell in cells}:
            doc[number - 1].apply_redactions(images=0, graphics=0, text=0)
        doc.save(str(candidate), incremental=True, encryption=fitz.PDF_ENCRYPT_KEEP)


def _find_phrase(page: Any, phrase: str, clip: Any) -> list[Any]:
    """Quads of ``phrase`` on the page within ``clip``; a phrase the layout
    wrapped is found in two halves."""
    quads = page.search_for(phrase, clip=clip, quads=True)
    if quads or len(phrase) < 4:
        return quads
    middle = len(phrase) // 2
    first, second = _find_phrase(page, phrase[:middle], clip), _find_phrase(page, phrase[middle:], clip)
    return first + second if first and second else []


def _split_around_images(page: Any, table: Any, nested: Any = ()) -> Any:
    """A cell holding a picture (a small table pasted as an image) becomes
    the part above it and the part below it: written as one block, the
    translation ran over the picture. A table drawn inside the cell
    (``nested``: the page's other tables) is kept clear the same way: taken
    into the cell's text, its numbers were run into the sentence above it
    ("数量5.5 5.5 1") and its own row was left blank."""
    import fitz

    images = [fitz.Rect(info["bbox"]) for info in page.get_image_info()]
    images += [fitz.Rect(rect) for rect in nested]
    # A figure drawn in vectors (a single-line diagram) is a picture too:
    # the many small strokes inside a cell, away from its own rules.
    drawn = [
        fitz.Rect(d["rect"]) for d in page.get_drawings()
        if d.get("rect") is not None and d["rect"].width < 0.85 * page.rect.width
    ]
    for cell in table.cells:
        if cell.rect is None:
            continue
        inner = fitz.Rect(cell.rect) + (3, 3, -3, -3)
        strokes = [r for r in drawn if inner.contains(r) and r.width < 0.9 * inner.width]
        if len(strokes) >= 8:
            figure = fitz.Rect(strokes[0])
            for r in strokes[1:]:
                figure |= r
            # A figure, not underlines or bars drawn through the text: most
            # of the cell's text lies outside it.
            lines = [
                fitz.Rect(line["bbox"]) for block in page.get_text("dict", clip=fitz.Rect(cell.rect)).get("blocks", ())
                for line in block.get("lines", ())
                if "".join(str(span.get("text", "")) for span in line.get("spans", ())).strip()
            ]
            covered = sum(1 for line in lines if figure.contains(fitz.Point((line.x0 + line.x1) / 2, (line.y0 + line.y1) / 2)))
            if figure.width > 40 and figure.height > 20 and lines and covered * 2 < len(lines):
                images.append(figure)
    if not images:
        return table
    cells: list[Any] = []
    changed = False
    for cell in table.cells:
        rect = fitz.Rect(cell.rect) if cell.rect is not None else None
        inside = [
            image for image in images
            if rect is not None and image.width > 20 and image.height > 20
            and rect.contains(fitz.Rect(image.x0 + 1, image.y0 + 1, image.x1 - 1, image.y1 - 1))
        ]
        # A column of one-line rows (a Gantt chart's task names, its bars
        # drawn beside them) is read line by line already.
        if not inside or cell.is_empty or _row_list_cells(page, SimpleNamespace(cells=[cell])):
            cells.append(cell)
            continue
        # The text between the pictures, in bands top to bottom.
        inside.sort(key=lambda box: box.y0)
        edges = [rect.y0]
        for image in inside:
            edges += [image.y0, image.y1]
        edges.append(rect.y1)
        bands = [(edges[i], edges[i + 1]) for i in range(0, len(edges), 2)]
        texts: list[list[str]] = [[] for _ in bands]
        for block in page.get_text("dict", clip=rect).get("blocks", ()):
            for line in block.get("lines", ()):
                text = "".join(str(span.get("text", "")) for span in line.get("spans", ())).strip()
                if not text:
                    continue
                middle = (line["bbox"][1] + line["bbox"][3]) / 2
                for index, (top, bottom) in enumerate(bands):
                    if top <= middle <= bottom:
                        texts[index].append(text)
        if not any(texts):
            cells.append(cell)
            continue
        changed = True
        for index, ((top, bottom), lines) in enumerate(zip(bands, texts)):
            if lines and bottom - top > 4:
                suffix = "ab"[index] if len(bands) == 2 else f"p{index}"
                cells.append(dataclasses.replace(cell, id=f"{cell.id}:{suffix}", rect=(rect.x0, top, rect.x1, bottom), text="\n".join(lines)))
    return dataclasses.replace(table, cells=tuple(cells)) if changed else table


# Source font of each table cell of the document being translated.
_CELL_SOURCE_FONTS: dict[str, str] = {}


def _dominant_font(page: Any, rect: Any) -> str:
    from collections import Counter

    import fitz

    counts: Counter[str] = Counter()
    for block in page.get_text("dict", clip=fitz.Rect(rect)).get("blocks", ()):
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                counts[str(span.get("font", ""))] += sum(1 for char in str(span.get("text", "")) if char.isalnum())
    return counts.most_common(1)[0][0] if counts else ""


def _cell_font_for(cell: Any, text: str) -> str:
    """The cell's font: in the style (serif or sans) of its source text."""
    from .pdf_layout import _font_file

    source = _CELL_SOURCE_FONTS.get(cell.id) or _CELL_SOURCE_FONTS.get(cell.id.rsplit(":", 1)[0], "")
    return _font_file(source, text, prefer_narrow=len(text) >= 80) or str(_table_cell_font(cell, text))


def _untranslated_cells(
    tables: list[Any], sent: set[str], row_cells: set[str], continued: dict[str, Any], source_language: str
) -> list[Any]:
    """Cells with text to translate that no path takes: not sent as a cell,
    not the next page's part of a sent cell, not read line by line."""
    handled = sent | row_cells | {continued[head].id for head in continued if head in sent}
    return [
        cell for table in tables for cell in table.cells
        if not cell.is_empty and cell.id not in handled and _needs_translation(cell.text, source_language)
    ]


def _continued_cells(tables: list[Any]) -> dict[str, Any]:
    """Cells of a row that runs on from one page's table into the next.

    A long clarification row ends mid-sentence at the foot of a page
    ("...the Tenderer shall procure,") and continues in the next page's
    first body row, whose row-number cell is empty. Translated apart, the
    model "completed" each half with an ellipsis ("其将采购……"); translated
    as one text, it reads as the source does. Maps head cell id -> tail cell.
    """
    pairs: dict[str, Any] = {}
    by_page: dict[int, list[Any]] = {}
    for table in tables:
        by_page.setdefault(table.page_number, []).append(table)
    for page_number, page_tables in by_page.items():
        following = by_page.get(page_number + 1)
        if not following:
            continue
        head = max(page_tables, key=lambda t: t.rect[1])
        tail = min(following, key=lambda t: t.rect[1])
        if head.column_count != tail.column_count:
            continue
        cells = lambda table, row: {c.column: c for c in table.cells if c.row == row}
        first_row = 1
        # A repeated header row on the next page is not the continuation.
        if [c.text for c in sorted(cells(tail, 1).values(), key=lambda c: c.column)] == [
            c.text for c in sorted(cells(head, 1).values(), key=lambda c: c.column)
        ]:
            first_row = 2
        last, carried = cells(head, head.row_count), cells(tail, first_row)
        if not last or not carried or 1 not in carried or 1 not in last:
            continue
        # The continuation row has no row number of its own.
        if not carried[1].is_empty or last[1].is_empty:
            continue
        for column, cell in carried.items():
            source = last.get(column)
            # Only a cell broken off mid-sentence ("...shall procure,") runs
            # on; one ending a sentence is complete, and merged with the next
            # list item ("b) ...") the model dropped a sentence of it.
            if (
                source is not None and not cell.is_empty and not source.is_empty and cell.rect is not None
                and source.text.rstrip()[-1:] not in ".;:!?。；：！？"
            ):
                pairs[source.id] = cell
    return pairs


# A line that starts a new item in a table cell: a numbered sub-item
# ("2.1.2 Liaison", "3.41."), a lettered or bracketed one ("a)", "(ii)",
# "C. Pump Station"), a bullet (also one on a line of its own, its text on
# the next: "▪" / "A treatment plant ..."), a percentage share ("70% of
# proportion ...") or a note.
_CELL_BULLETS = "-*•●▪■□◆◇○►➢\uf0a7\uf0b7"
_CELL_ITEM_START = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*[.)]\s|\d+(?:\.\d+){1,}\.?(?=\s?[A-Za-z])|\(?[a-zA-Z]\)|[A-Z]\.\s(?=[A-Z])|\(?[ivxIVX]{1,4}\)|[" + _CELL_BULLETS + r"](?:\s|$)"
    r"|\d+(?:\.\d+)?\s?%\s|(?:Note|NOTE|Notes)\s*:)"
)


_CLAUSE_HEADING_RE = re.compile(r"^[A-Z][A-Za-z-]*\s+\d+(?:\.\d+)+\.?(?:\s|$)")


def _structure_cell_text(text: str) -> str:
    """Rejoin a cell's visual line wraps; keep only the breaks between items.

    A PDF cell's text comes line by line as the cell wrapped it. Sent like
    that, the model cannot tell a wrap from a new item: it ran the milestone
    sub-items 2.1.1 / 2.1.2 / 2.1.3 into one sentence, and a half sentence
    cut off at a wrap ("70% of proportion of each site on delivery of" /
    "complete equipment") was at times copied back in English. Each item
    now reaches it as one whole line.
    """
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    if len(lines) < 2:
        return text
    # "Merge Tiles: yes" / "Method: Inverse Distance": a run of key: value
    # lines is a list of entries, one per line (run together, "是方法：反距离").
    keyed = [_KEY_VALUE_RE.match(line) is not None for line in lines]
    out = [lines[0]]
    for index, line in enumerate(lines[1:], start=1):
        key_value = keyed[index] and (any(keyed[:index]) or (index + 1 < len(lines) and keyed[index + 1]))
        # A clause heading on its own line ("Sub-Clause 4.2.1" / "The first
        # sentence of ..."): run into the text, the headings of a cell's
        # clauses disappeared into one paragraph.
        heading = (
            len(lines[index - 1]) <= 25 and re.search(r"\d\.\d+\.?$", lines[index - 1]) is not None and line[:1].isupper()
        ) or (
            # ... and the next clause's heading after a finished sentence
            _CLAUSE_HEADING_RE.match(line) is not None and re.search(r"[.:;\"”']$", lines[index - 1]) is not None
        )
        if _CELL_ITEM_START.match(line) or key_value or heading:
            out.append(line)
        elif _CJK_RE.search(out[-1][-1:]) or _CJK_RE.search(line[:1]):
            out[-1] += line
        else:
            out[-1] += " " + line
    return "\n".join(out)


_ITEM_NUMBER_AT_LINE_START = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+)+\.?)(?=\s*\S)")


def _restore_item_breaks(source: str, translation: str) -> str:
    """Put each numbered sub-item of a cell back on its own line.

    A milestone cell lists "2.1.1. Inception Report Approved" / "2.1.2
    Liaison..." on separate lines; the model returned one run-on sentence
    ("…获批2.1.2.与利益相关者…"). Only numbers that start a line in the
    source are used, so a reference like "第2.1.1款" is never split.
    """
    translation = _restore_bullet_breaks(source, translation)
    numbers = _ITEM_NUMBER_AT_LINE_START.findall(source)
    if len(numbers) < 2:
        return translation
    for number in numbers[1:]:
        translation = re.sub(
            rf"(?<=[^\n])[ \t]*(?=(?<![0-9.]){re.escape(number)}(?![0-9]))", "\n", translation, count=1
        )
    return translation


def _restore_bullet_breaks(source: str, translation: str) -> str:
    """Each bulleted item of a cell on a line of its own again.

    "▪ A treatment plant ..." / "▪ Proper sludge ..." came back as one run
    ("…进行处理；・ 将根据…"). Only when the translation has exactly as many
    bullets as the source has items, so a middle dot inside a name is
    never taken for one.
    """
    marks = [line.strip()[0] for line in source.splitlines() if line.strip()[:1] and line.strip()[0] in _CELL_BULLETS[2:]]
    if len(marks) < 2:
        return translation
    for mark in dict.fromkeys([marks[0], "•", "·", "・", "▪"]):
        if translation.count(mark) == len(marks):
            return re.sub(rf"(?<=\S)[ \t]*(?={re.escape(mark)})", "\n", translation)
    return translation


_SPLIT_MARKS = "。；，、;,. "


_SENTENCE_END_RE = re.compile(r"[.!?:;。！？：；…][\"'”’)）]*$")


def _across_pages(paragraphs: list[_Paragraph], page_heights: dict[int, float]) -> dict[int, int]:
    """Index of a page's last body paragraph -> the next page's first one,
    where that paragraph runs on over the page break.

    The page's last paragraph stops mid-sentence on a full line (no closing
    punctuation, its last line reaching the margin), and the next page's
    first paragraph continues in the same size, from the same margin, with
    no label or bullet, not centred, and not starting a sentence.
    """
    # Running headers and footers ("Page 9 of 213") are the same text, but
    # for its numbers, on many pages: a fixed margin missed a footer set at
    # 91% of the page, which then counted as every page's last paragraph.
    from collections import Counter

    def signature(paragraph: _Paragraph) -> str:
        return re.sub(r"\d+", "#", " ".join(paragraph.text.split()))

    pages_with = Counter()
    for signature_text, page_number in {(signature(p), p.page_number) for p in paragraphs}:
        pages_with[signature_text] += 1
    page_count = len({p.page_number for p in paragraphs})
    furniture = {text for text, count in pages_with.items() if count >= max(3, 0.3 * page_count)}

    def body(index: int) -> bool:
        paragraph = paragraphs[index]
        height = page_heights.get(paragraph.page_number, 842.0)
        return (
            0.07 * height < paragraph.bbox[1] and paragraph.bbox[3] < 0.95 * height
            and not paragraph.centred and signature(paragraph) not in furniture
        )

    by_page: dict[int, list[int]] = {}
    for index, paragraph in enumerate(paragraphs):
        if body(index):
            by_page.setdefault(paragraph.page_number, []).append(index)
    result: dict[int, int] = {}
    for page_number, indices in by_page.items():
        following = by_page.get(page_number + 1)
        if not following:
            continue
        head = max(indices, key=lambda index: paragraphs[index].bbox[3])
        tail = min(following, key=lambda index: paragraphs[index].bbox[1])
        a, b = paragraphs[head], paragraphs[tail]
        last, first = a.lines[-1], b.lines[0]
        text = a.text.rstrip()
        if (
            not _SENTENCE_END_RE.search(text)
            and a.bounds[1] - last.bbox[2] <= 1.5 * last.size
            and abs(a.size - b.size) <= 0.6
            and abs(first.bbox[0] - a.bbox[0]) <= max(2.0, 0.8 * a.size)
            and not first.bulleted
            and _label_text_start(first) is None
            and not bool(_LABEL_RE.match(first.text))
            # A capital starts a sentence unless the page ended inside one
            # ("... the realigned route of Line B &" / "Line C ...").
            and (not first.text[:1].isupper() or re.search(r"(?:[a-z]+|[,&(/-])$", text))
        ):
            result[head] = tail
    return result


def _split_continued(translation: str, share: float) -> tuple[str, str]:
    """Split one translation back over a row's two page parts, near the
    source's own proportion and at a punctuation mark where one is close."""
    target = round(len(translation) * share)
    window = max(4, len(translation) // 6)
    best = None
    for offset in range(window + 1):
        for index in (target + offset, target - offset):
            if 0 < index < len(translation) and translation[index - 1] in _SPLIT_MARKS:
                best = index
                break
        if best is not None:
            break
    if best is None:
        best = min(max(target, 1), len(translation) - 1)
        # Never cut through a Latin word or a number.
        while 0 < best < len(translation) and translation[best - 1].isascii() and translation[best - 1].isalnum() and translation[best].isascii() and translation[best].isalnum():
            best += 1
    head, tail = translation[:best].strip(), translation[best:].strip()
    return (head or translation, tail or translation) if not (head and tail) else (head, tail)


def _page_key(page: Any) -> tuple[int, int]:
    return round(page.rect.width), round(page.rect.height)


def _content_bounds(doc: Any, tables: list[Any]) -> dict[tuple[int, int], tuple[float, float]]:
    """Left/right text margins of the body text, per page size.

    Computed separately for each page size: a portrait cover in front of
    landscape body pages (a real tender document) has completely different
    margins, and one document-wide value made every centred cover line look
    off-centre and let translations run past the narrower page's edge.
    """
    import fitz

    lefts: dict[tuple[int, int], list[float]] = {}
    rights: dict[tuple[int, int], list[float]] = {}
    for page_number, page in enumerate(doc, start=1):
        key = _page_key(page)
        lefts.setdefault(key, [])
        rights.setdefault(key, [])
        table_rects = [fitz.Rect(t.rect) for t in tables if t.page_number == page_number]
        lines, _ = _visual_lines(page, table_rects)
        for line in lines:
            x0, y0, x1, y1 = line.bbox
            if y0 > page.rect.height * 0.92 or y1 < page.rect.height * 0.04:
                continue
            lefts[key].append(x0)
            rights[key].append(x1)
    bounds: dict[tuple[int, int], tuple[float, float]] = {}
    for key in lefts:
        width = key[0]
        if not lefts[key]:
            bounds[key] = (width * 0.12, width * 0.88)
            continue
        # With plenty of body lines the right margin is the edge most lines
        # reach (justified text), ignoring punctuation overhang; with only a
        # few (a cover page) the widest line is the best evidence there is.
        values = sorted(rights[key])
        right = max(values[int(len(values) * 0.9)], values[-1] - 10.0) if len(values) >= 20 else values[-1]
        bounds[key] = (min(lefts[key]), min(right, width))
    return bounds


def _visual_lines(page: Any, table_rects: list[Any]) -> tuple[list[_VisualLine], int]:
    """Horizontal text lines outside tables, with same-baseline fragments merged."""
    import fitz

    segments: list[_Segment] = []
    markers: list[tuple[float, float, float, float]] = []
    rotated = 0
    # rawdict: the per-glyph boxes let removal target exactly these glyphs.
    for block in page.get_text("rawdict").get("blocks", ()):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", ()):
            spans = []
            for span in line.get("spans", ()):
                # Fake bold prints each glyph twice, a fraction of a point
                # apart ("MMTTBBMM"): the copy is read once (and still removed).
                kept = []
                for char in span.get("chars", ()):
                    if kept and char.get("c") == kept[-1].get("c") and abs(char["bbox"][0] - kept[-1]["bbox"][0]) < 0.3 * max(0.1, kept[-1]["bbox"][2] - kept[-1]["bbox"][0]):
                        continue
                    kept.append(char)
                span["text"] = "".join(str(char.get("c", "")) for char in kept)
                if not str(span.get("text", "")).strip():
                    continue
                # Map symbols ("!(" in ESRIDefaultMarker is a point marker)
                # are graphics: rewriting them in a text font printed "!("
                # and their removal clipped neighbouring labels.
                if _SYMBOL_FONT_RE.search(str(span.get("font", ""))):
                    markers.append(tuple(span["bbox"]))
                    continue
                spans.append(span)
            if not spans:
                continue
            direction = line.get("dir", (1.0, 0.0))
            # Callout labels on drawings are often tilted by a fraction of a
            # degree; up to ~2 degrees they are written back horizontally.
            if float(direction[0]) < 0.999 or abs(float(direction[1])) > 0.035:
                rotated += 1
                continue
            # Only the visible spans: a trailing blank span widens the line
            # and hides that it is centred.
            box = fitz.Rect(spans[0]["bbox"])
            for span in spans[1:]:
                box |= fitz.Rect(span["bbox"])
            # The line starts at its first visible glyph: a heading padded
            # with leading spaces starts where its text does, not 18pt left
            # of the body text.
            visible = [char["bbox"][0] for span in spans for char in span.get("chars", ()) if str(char.get("c", "")).strip()]
            if visible:
                box.x0 = max(box.x0, min(visible))
            centre =fitz.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
            if any(rect.contains(centre) for rect in table_rects):
                continue
            # Blank spans are dropped above, so a space that was its own span
            # ("before" + " " + bold "January 29") must come back from the gap.
            text = str(spans[0].get("text", ""))
            for prev, span in zip(spans, spans[1:]):
                piece = str(span.get("text", ""))
                gap = span["bbox"][0] - prev["bbox"][2]
                if gap > 0.1 * float(span.get("size") or 10.0) and not text.endswith(" ") and not piece.startswith(" "):
                    text += " "
                text += piece
            # A second copy of the same text over the first (fake bold drawn
            # as two whole strings) is read once; its glyphs are still erased.
            copy_of = next(
                (other for other in segments
                 if other.text == text and fitz.Rect(other.bbox).intersect(box).get_area() >= 0.8 * box.get_area()),
                None,
            )
            if copy_of is not None:
                copy_of.spans.extend(spans)
                continue
            segments.append(_Segment(text, tuple(box), spans))
    # Visible vertical rules: pieces of text on either side of one sit in
    # different cells (a Gantt chart's "1st Half" | "2nd Half" headers were
    # joined into one line and translated as one overflowing string).
    from .pdf_table import _visible_rules

    vertical_rules = _visible_rules(page)[0]
    # Boxes text sits in (a diagram's boxes, a slide's coloured panels):
    # pieces in different boxes are different texts.
    boxes = [
        fitz.Rect(drawing["rect"]) for drawing in page.get_drawings()
        if drawing.get("rect") is not None and drawing.get("fill") is not None
        # a visible fill: white boxes behind each line (Word shading) are not boxes
        and any(value < 0.9 for value in drawing["fill"])
        and drawing["rect"].width > 20 and drawing["rect"].height > 10
        and drawing["rect"].width < 0.9 * page.rect.width
    ]

    def box_of(bbox: tuple[float, float, float, float]) -> int | None:
        centre = fitz.Point((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)
        inside = [index for index, box in enumerate(boxes) if box.contains(centre)]
        return min(inside, key=lambda index: boxes[index].get_area()) if inside else None
    # Column edges: where several pieces of text on the page start. A piece
    # starting there is a column of its own ("1388 g" beside "Weight
    # (Battery & Propellers"), even when the gap before it is small.
    from collections import Counter

    starts = Counter(round(segment.bbox[0]) for segment in segments)
    lines: list[_VisualLine] = []
    for segment in sorted(segments, key=lambda s: (s.bbox[1], s.bbox[0])):
        for line in lines:
            lx0, ly0, lx1, ly1 = line.bbox
            height = min(ly1 - ly0, segment.bbox[3] - segment.bbox[1])
            overlap = min(ly1, segment.bbox[3]) - max(ly0, segment.bbox[1])
            # Same baseline only joins nearby text: labels scattered across
            # a drawing share baselines but are separate pieces of text.
            size = max(float(span.get("size", 0.0)) for span in segment.spans)
            gap = max(segment.bbox[0] - lx1, lx0 - segment.bbox[2])
            between = (min(lx1, segment.bbox[2]), max(lx0, segment.bbox[0]))
            marker_between = any(
                between[0] <= (m[0] + m[2]) / 2 <= between[1]
                and min(m[3], segment.bbox[3]) - max(m[1], segment.bbox[1]) > 0
                for m in markers
            )
            middle = (segment.bbox[1] + segment.bbox[3]) / 2
            rule_between = any(between[0] <= x <= between[1] and y0 <= middle <= y1 for x, y0, y1 in vertical_rules)
            # The left piece and the right one, whichever was read first (a
            # right-hand header set a point higher is read before the left).
            if segment.bbox[0] > lx1:
                left_text, left_end, right_start = " ".join(other.text for other in line.segments).strip(), lx1, segment.bbox[0]
            else:
                left_text, left_end, right_start = segment.text.strip(), segment.bbox[2], lx0
            column_start = (
                right_start > left_end and gap > 0.5 * size
                # a list label ("1.", "viii.") belongs with its text
                # (only the label itself: "27. Tender Opening" is a side
                # heading of its own beside clause "27.1")
                and not _LIST_LABEL_RE.match(left_text) and not _LABEL_RE.fullmatch(left_text)
                and not re.fullmatch(r"\d+(?:\.\d+)*\.?", left_text)  # section number "4.1"
                and (
                    sum(count for x, count in starts.items() if abs(x - right_start) <= 1.5) >= 4
                    # ... or it starts just past an edge another line ends at
                    # (a running header justified to half the page: "...
                    # Management" / "... Larechs", then "Section 6 - ...")
                    or sum(
                        1 for other in segments
                        if abs(other.bbox[2] - left_end) <= 1.0
                        and not min(ly0, segment.bbox[1]) <= (other.bbox[1] + other.bbox[3]) / 2 <= max(ly1, segment.bbox[3])
                    ) >= 1
                )
            )
            own_box = box_of(segment.bbox)
            other_box = box_of(line.segments[-1].bbox)
            rule_between = rule_between or column_start or (own_box is not None and other_box is not None and own_box != other_box)
            if height > 0 and overlap >= 0.6 * height and gap <= 3.0 * size and not marker_between and not rule_between:
                line.segments.append(segment)
                break
        else:
            lines.append(_VisualLine([segment]))
    lines = _join_spread_line(lines, vertical_rules)
    for line in lines:
        index = box_of(line.bbox)
        if index is not None and all(box_of(segment.bbox) == index for segment in line.segments):
            line.box = tuple(boxes[index])
    # Only a small box is one text (a diagram node of a line or three); a
    # legend panel or a page-wide background holds many separate ones.
    from collections import Counter

    members = Counter(line.box for line in lines if line.box is not None)

    def centred_in_box(line: _VisualLine) -> bool:
        middle = (line.box[0] + line.box[2]) / 2
        return abs((line.bbox[0] + line.bbox[2]) / 2 - middle) <= max(4.0, 0.5 * line.size)

    # ... and its text is centred in it, as a node's is (a legend's entries
    # in a small panel are separate labels, set from the left).
    centred_boxes = {box for box in members if all(centred_in_box(line) for line in lines if line.box == box)}
    for line in lines:
        if line.box is not None and (members[line.box] > 3 or line.box not in centred_boxes):
            line.box = None
        x0, y0, _, y1 = line.bbox
        line.bulleted = any(
            m[2] <= x0 + 1.0 and x0 - m[2] <= 3.0 * line.size
            and min(m[3], y1) - max(m[1], y0) >= 0.5 * min(m[3] - m[1], y1 - y0)
            for m in markers
        )
    lines.sort(key=lambda line: (line.bbox[1], line.bbox[0]))
    return lines, rotated


def _join_spread_line(
    lines: list[_VisualLine], vertical_rules: list[tuple[float, float, float]]
) -> list[_VisualLine]:
    """Pieces of one justified line spread far apart are one line again.

    'marked      "WITHDRAWAL,"      "SUBSTITUTION,"' is justified with gaps
    wider than three letters, so it was read as three texts and the quoted
    words were translated and set on their own, scattered across the line.
    Such a run spans exactly the line above it, the previous line of the
    same justified paragraph; a running header's two halves ("... Project"
    and "Section 1 - ...") or labels on a drawing that share a baseline do not.
    """

    def spans_line_above(run: list[_VisualLine]) -> bool:
        x0, x1, top, size = run[0].bbox[0], run[-1].bbox[2], run[0].bbox[1], run[0].size
        return any(
            abs(other.bbox[0] - x0) <= 1.5 and abs(other.bbox[2] - x1) <= 1.5
            and 0 < top - other.bbox[1] <= 2.0 * size
            for other in lines
        )

    order = sorted(lines, key=lambda line: (round(line.baseline, 1), line.bbox[0]))
    result: list[_VisualLine] = []
    index = 0
    while index < len(order):
        run = [order[index]]
        for line in order[index + 1:]:
            last = run[-1]
            gap = line.bbox[0] - last.bbox[2]
            if not (
                abs(line.baseline - last.baseline) <= 0.5 and abs(line.size - last.size) <= 0.1
                and 0 < gap <= 6.0 * last.size
                and not any(last.bbox[2] <= x <= line.bbox[0] and y0 <= line.baseline <= y1 for x, y0, y1 in vertical_rules)
            ):
                break
            run.append(line)
        if len(run) > 1 and spans_line_above(run):
            result.append(_VisualLine([segment for line in run for segment in line.segments]))
            index += len(run)
        else:
            result.append(order[index])
            index += 1
    return result


def _body_edges(lines: list[_VisualLine], bounds: tuple[float, float]) -> tuple[float, float]:
    """The edges this page's body text starts and ends at.

    The margins are the extremes of every page of this size; a body set
    inside them (text from 90pt to 527pt where other pages reach 36pt and
    549pt) made every line look indented and ended early, so each line was
    translated on its own, and justified paragraphs were set as centred. Most lines of a body share both edges (justified
    text); on a drawing, where labels share an edge only by chance (no edge
    is shared by a third of the lines), the margins stand.
    """
    from collections import Counter

    left, right = bounds
    if len(lines) >= 8:
        edge, count = Counter(round(line.bbox[0]) for line in lines).most_common(1)[0]
        if count >= max(4, 0.3 * len(lines)) and edge - 1.0 > left:
            left = edge - 1.0
        edge, count = Counter(round(line.bbox[2]) for line in lines).most_common(1)[0]
        if count >= max(4, 0.3 * len(lines)) and edge + 1.0 < right:
            right = edge + 1.0
    return left, right


def _stretched(line: _VisualLine) -> bool:
    """Word spaces widened by justification (wider than 0.4 of the size)."""
    chars = sorted((c for span in line.spans for c in span.get("chars", ())), key=lambda c: c["bbox"][0])
    gaps: list[float] = []
    end = None
    after_space = False
    for char in chars:
        if not str(char.get("c", "")).strip():
            after_space = end is not None
            continue
        if after_space and end is not None:
            gaps.append(char["bbox"][0] - end)
        after_space = False
        end = char["bbox"][2]
    # Justification widens every space; one wide tab after a heading's
    # number ("1.10    TOTAL CATCHMENT AREA") is not that.
    return len(gaps) >= 3 and sorted(gaps)[len(gaps) // 2] > 0.4 * line.size


def _box_order(lines: list[_VisualLine]) -> list[_VisualLine]:
    """Lines of one box follow each other, where the box's first line is."""
    ordered: list[_VisualLine] = []
    placed: set[int] = set()
    for line in lines:
        if id(line) in placed:
            continue
        group = [line] if line.box is None else [other for other in lines if other.box == line.box]
        for member in group:
            if id(member) not in placed:
                placed.add(id(member))
                ordered.append(member)
    return ordered


def _reading_order(lines: list[_VisualLine], bounds: tuple[float, float]) -> list[_VisualLine]:
    """Two columns read one after the other, not line by line across both.

    Read strictly top to bottom, a brochure's left and right columns
    alternated ("Sewerage system from LARECHS Colony" / "14,165 Million PKR"
    / "to Gulshan-e-Ravi ...") and every line became a paragraph. A gutter
    is a vertical band in the middle of the page that no line of either
    column crosses; between lines that do cross it (titles, section bars
    across both columns) the left column is read before the right.
    """
    gutter = _gutter(lines, bounds)
    if gutter is None:
        return _stack_order(lines)
    ordered: list[_VisualLine] = []
    band: list[_VisualLine] = []

    def flush() -> None:
        ordered.extend(sorted((l for l in band if l.bbox[2] <= gutter), key=lambda l: (l.bbox[1], l.bbox[0])))
        ordered.extend(sorted((l for l in band if l.bbox[2] > gutter), key=lambda l: (l.bbox[1], l.bbox[0])))
        band.clear()

    for line in sorted(lines, key=lambda l: (l.bbox[1], l.bbox[0])):
        if line.bbox[0] < gutter < line.bbox[2]:
            flush()
            ordered.append(line)
        else:
            band.append(line)
    flush()
    return ordered


def _stack_order(lines: list[_VisualLine]) -> list[_VisualLine]:
    """Lines stacked under each other (the same left or right edge, a line
    apart) are read one after the other, before the stack beside them.

    A running header in two blocks ("Lahore ... (LWDMP) -" / "Sewerage
    System ..." on the left, "Part III- Conditions of Contract and" /
    "Contract Forms" right-aligned beside it) read strictly top to bottom
    alternated between the blocks, and every line became a paragraph.
    """
    order = sorted(lines, key=lambda line: (line.bbox[1], line.bbox[0]))
    placed: set[int] = set()
    result: list[_VisualLine] = []
    for line in order:
        if id(line) in placed:
            continue
        placed.add(id(line))
        result.append(line)
        last = line
        while True:
            below = [
                other for other in order
                if id(other) not in placed and abs(other.size - last.size) <= 0.6
                and 0 < other.bbox[1] - last.bbox[1] <= 1.6 * last.size
                and (abs(other.bbox[0] - last.bbox[0]) <= 1.5 or abs(other.bbox[2] - last.bbox[2]) <= 1.5)
            ]
            # Only where a line of another stack lies between: elsewhere the
            # order is the page's own.
            if not below:
                break
            following = min(below, key=lambda other: other.bbox[1])
            between = [
                other for other in order
                if id(other) not in placed and other is not following and last.bbox[1] <= other.bbox[1] < following.bbox[1]
            ]
            if between and any(min(o.bbox[2], following.bbox[2]) - max(o.bbox[0], following.bbox[0]) > 0 for o in between):
                break  # a line in between overlaps the stack: not side by side
            placed.add(id(following))
            result.append(following)
            last = following
    return result


def _gutter(lines: list[_VisualLine], bounds: tuple[float, float]) -> float | None:
    """x of a vertical band between two columns that few lines cross."""
    # A table's rows are no text columns: the short lines of an unruled
    # price table made a page of footnotes "two columns", and a footnote
    # ending short of that gutter ran into the next one.
    lines = [line for line in lines if not line.in_cell]
    left, right = bounds
    width = right - left
    if len(lines) < 8 or width <= 0:
        return None
    best: tuple[int, float] | None = None
    for step in range(25, 76):
        gutter = left + width * step / 100
        crossing = sum(1 for line in lines if line.bbox[0] < gutter < line.bbox[2])
        left_side = sum(1 for line in lines if line.bbox[2] <= gutter)
        right_side = sum(1 for line in lines if line.bbox[0] >= gutter)
        if left_side >= 4 and right_side >= 4 and crossing <= 0.25 * len(lines):
            score = min(left_side, right_side) - crossing
            if best is None or score > best[0]:
                best = (score, gutter)
    if best is None:
        # Side headings in a column left of the body's edge ("24. Deadline
        # for" / "Submission of" / "Tenders" beside clause 24.1): read line
        # by line across both, each heading line became a paragraph of its own.
        side = [line for line in lines if line.bbox[2] < left - 0.5 * line.size]
        if len(side) >= 2:
            gutter = max(line.bbox[2] for line in side) + 0.5
            crossing = sum(1 for line in lines if line.bbox[0] < gutter < line.bbox[2])
            right_side = sum(1 for line in lines if line.bbox[0] >= gutter)
            if right_side >= 4 and crossing <= 0.25 * len(lines):
                return gutter
    return None if best is None else best[1]


def _is_centred(line: _VisualLine, bounds: tuple[float, float]) -> bool:
    left, right = bounds
    x0, _, x1, _ = line.bbox
    return (
        abs((x0 + x1) / 2 - (left + right) / 2) <= max(4.0, line.size * 0.5)
        and x0 - left > line.size * 0.5
        and right - x1 > line.size * 0.5
    )


def _is_drawing_frame(table: Any, page: Any) -> bool:
    """A detected "table" whose one cell covers most of the page is a
    drawing/map frame (with a title block), not a table: its labels are
    separate texts, not cell content to be reflowed."""
    import fitz

    area = page.rect.width * page.rect.height
    if any(c.rect is not None and fitz.Rect(c.rect).get_area() > 0.5 * area for c in table.cells):
        return True
    # A floor plan's rooms found as a "table": its cells overlap one
    # another (a room inside another's rectangle), which no real grid does,
    # and two rooms' labels were read as one cell ("办公室办公室").
    filled = [fitz.Rect(c.rect) for c in table.cells if c.rect is not None and not c.is_empty]
    # ... and most of its "cells" are empty rooms: a schedule whose bars
    # overlap its own grid still has text in a good share of its cells.
    if len(filled) > 0.2 * len(table.cells):
        return False
    for index, one in enumerate(filled):
        for other in filled[index + 1:]:
            smaller = min(one.get_area(), other.get_area())
            if smaller > 0 and (one & other).get_area() > 0.5 * smaller:
                return True
    return False


def _cuts_through_text(table: Any) -> bool:
    """A "grid" with a cell lower than the text in it is no table.

    A callout box beside a running header was found as a table whose first
    cell was 6pt tall around a 9pt header line; one font size fits every
    cell of a page's tables, so that cell set the whole page at 3pt.
    """
    return any(
        cell.rect is not None and not cell.is_empty and cell.source_font_size
        and cell.rect[3] - cell.rect[1] < 0.8 * cell.source_font_size
        for cell in table.cells
    )


def _without_spanning_cells(table: Any) -> Any:
    """Drop a cell whose rectangle holds other filled cells of its table.

    Such a cell (the whole grid plus the text above it, found from a box
    drawn round the table) repeats every other cell's text; written as one
    block it covered the table, and took the sentence above it along.
    """
    import fitz

    filled = [c for c in table.cells if c.rect is not None and not c.is_empty]

    def holds_others(cell: Any) -> bool:
        rect = fitz.Rect(cell.rect)
        return any(
            other is not cell and (rect & fitz.Rect(other.rect)).get_area() >= 0.9 * fitz.Rect(other.rect).get_area() > 0
            for other in filled
        )

    spanning = {cell.id for cell in filled if holds_others(cell)}
    if not spanning:
        return table
    return dataclasses.replace(table, cells=tuple(c for c in table.cells if c.id not in spanning))


def _horizontal_rules(page: Any) -> list[tuple[float, float, float, float]]:
    """Thin horizontal strokes/fills on the page (underlines, separator rules)."""
    rules = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect is not None and rect.height <= 2.0 and rect.width > 2.0 * max(rect.height, 1.0):
            rules.append((rect.x0, rect.y0, rect.x1, rect.y1))
    return rules


def _underlined(line: _VisualLine, rules: list[tuple[float, float, float, float]]) -> bool:
    x0, _, x1, y1 = line.bbox
    width = x1 - x0
    return any(
        y1 - 0.4 * line.size <= ry0 <= y1 + 0.6 * line.size
        and min(x1, rx1) - max(x0, rx0) >= 0.8 * width
        for rx0, ry0, rx1, _ in rules
    )


def _segment(
    page_number: int,
    lines: list[_VisualLine],
    bounds: tuple[float, float],
    rules: list[tuple[float, float, float, float]] = (),
) -> list[_Paragraph]:
    """Group visual lines into paragraphs from the source's own layout signals.

    A line with its own underline ends its paragraph: the rule belongs to
    that line, and reflowing several underlined lines would leave the rules
    crossing the new text.
    """
    left, right = bounds
    lines = _box_order(_reading_order(lines, bounds))
    paragraphs: list[_Paragraph] = []

    gutter = _gutter(lines, bounds)

    def column_right(line: _VisualLine) -> float:
        """A left-column line's column ends at the gutter; otherwise the margin."""
        return gutter if gutter is not None and line.bbox[2] <= gutter else right

    current: list[_VisualLine] = []
    current_centred = False
    for line in lines:
        centred = own_centred = _is_centred(line, bounds)
        # A list item whose text starts where other lines do is aligned to
        # that edge ("ii. Provide technical directions..." happened to sit
        # in the middle of the margins and was set centred).
        if centred and line.bulleted:
            centred = own_centred = False
        if centred:
            text_start = _label_text_start(line)
            if text_start is not None and sum(
                1 for other in lines
                if other is not line and (abs(other.bbox[0] - text_start) <= 1.5 or abs((_label_text_start(other) or -99.0) - text_start) <= 1.5)
            ) >= 1:
                centred = own_centred = False
        # The widest line of a centred block is what sets the margins, so it
        # has no open margins of its own; it must not split that block.
        if not centred and current and current_centred and (line.bbox[2] - line.bbox[0]) >= 0.95 * (right - left):
            centred = True
        # A list item's next line starts where its text does, after a short
        # label set apart ("viii." right-aligned, text at 101pt): it continues
        # the item whatever the label style, and is never centred.
        hanging = bool(current) and _continues_hanging_item(current[0], line)
        if hanging:
            centred = own_centred = current_centred
        if current and current[-1].box is not None and line.box == current[-1].box and abs(line.size - current[-1].size) <= 0.6:
            # Lines of one box are one text (a diagram node "1. Collection" /
            # "System"), whatever their alignment.
            current.append(line)
            continue
        if current and current[-1].box is not None and line.box is not None and current[-1].box != line.box:
            paragraphs.append(_Paragraph(page_number, current, current_centred))
            current = []
            current_centred = centred
            current.append(line)
            continue
        if current:
            previous = current[-1]
            size = previous.size
            new = (
                abs(line.size - size) > 0.6
                or line.color != previous.color
                or line.bbox[1] - previous.bbox[1] > 2.0 * size
                or line.bbox[1] < previous.bbox[1]
                or centred != current_centred
                # A centred bold title over centred plain lines ("PHYSICAL
                # PHASING OF PROJECT" / "Total Proposed Sewer Length= ...")
                # is a title and its own paragraph, not one sentence.
                or (centred and _is_bold(_Paragraph(0, [line], True)) != _is_bold(_Paragraph(0, [previous], True)))
                or bool(_LABEL_RE.match(line.text))
                # Symbol-font bullets are set aside as markers, so a list of
                # justified items read as one paragraph and two items were
                # translated as one sentence.
                or line.bulleted
                # a contents entry ends at its page number; a list label
                # starts the next item ("i." / "ii." right-aligned)
                or _TOC_ENTRY_RE.match(previous.text) is not None
                or (_label_text_start(line) is not None and not hanging)
                # "S-mode: 6 m/s" / "P-mode: 5 m/s": entries of a list of
                # key: value lines, one per line.
                or (_KEY_VALUE_RE.match(line.text) is not None and _KEY_VALUE_RE.match(previous.text) is not None)
                # Space before a paragraph: the gap to this line is wider
                # than the paragraph's own line pitch (16.8pt after lines
                # 13.8pt apart).
                or (
                    len(current) >= 2
                    and (line.baseline - previous.baseline) - (previous.baseline - current[-2].baseline) > 0.2 * size
                )
                or _underlined(previous, rules)
                # lines of one paragraph sit under each other
                or min(line.bbox[2], previous.bbox[2]) - max(line.bbox[0], previous.bbox[0]) <= 0
            )
            if not new and not centred:
                indented = line.bbox[0] - left > 0.8 * size  # first-line indent
                # Body text (in the left quarter of the text area) is
                # indented only against the line above it: on a page whose
                # lines start at 65, 72, 90 and 108pt there is no margin to
                # measure from, and every line counted as indented. Text
                # further right (a legend column) keeps the margin rule.
                if line.bbox[0] - left <= 0.25 * (right - left):
                    indented = line.bbox[0] - previous.bbox[0] > 0.8 * size
                # A numbered item's continuation lines hang under its text
                # ("1. In partial..." / "   Section 2..."), they do not
                # start new paragraphs.
                first = current[0]
                if indented and _LABEL_RE.match(first.text) and first.bbox[0] < line.bbox[0] <= first.bbox[0] + 4.0 * size:
                    hang = current[1].bbox[0] if len(current) > 1 else line.bbox[0]
                    indented = abs(line.bbox[0] - hang) > 1.5
                # A right-aligned block (a running header) has ragged left
                # edges; split, its first line came back as "从...的污水系统".
                if indented and previous.bbox[0] - left > 0.8 * size and abs(line.bbox[2] - previous.bbox[2]) <= 2.0:
                    indented = False
                # The short last line of an indented, justified block (a list
                # item's text) starts under the lines above it; two justified
                # lines are the evidence -- after one, a map legend's next
                # entry looks the same.
                if indented and hanging:
                    indented = False
                # A bulleted item's next line starts under its text.
                if indented and current[0].bulleted and abs(line.bbox[0] - current[0].bbox[0]) <= 1.5:
                    indented = False
                if (
                    indented and len(current) >= 2
                    and abs(line.bbox[0] - previous.bbox[0]) <= 1.5
                    and all(abs(c.bbox[2] - current[0].bbox[2]) <= 2.0 and abs(c.bbox[0] - previous.bbox[0]) <= 1.5 for c in current[-2:])
                ):
                    indented = False
                # Ended early against the right edge of its own column: on a
                # two-column page every line of the left column ended 300pt
                # short of the page margin and became its own paragraph.
                edge = min(right, column_right(previous))
                ended_early = edge - previous.bbox[2] > 1.5 * size
                # Justified to a narrower measure (text in a box): the next
                # line ends exactly where this long one does.
                long_line = previous.bbox[2] - previous.bbox[0] >= 0.6 * (edge - previous.bbox[0])
                if ended_early and (
                    _stretched(previous)  # spaced out to fill its measure
                    or (abs(line.bbox[2] - previous.bbox[2]) <= 1.5 and _stretched(line) and long_line)
                ):
                    ended_early = False
                # The paragraph's own measure: the lines before ended here too.
                if ended_early and len(current) >= 2 and abs(current[-2].bbox[2] - previous.bbox[2]) <= 1.5:
                    ended_early = False
                # ... or the next line, or other lines of the page, end here:
                # footnotes under a table reach 526pt where the document's
                # margin is 555pt (a wide table elsewhere), and each
                # footnote's second line ("Labor wages") was set apart as a
                # paragraph of its own.
                if ended_early and long_line and (
                    abs(line.bbox[2] - previous.bbox[2]) <= 1.5
                    or sum(
                        1 for other in lines
                        if other is not previous and abs(other.size - previous.size) <= 0.6
                        and abs(other.bbox[2] - previous.bbox[2]) <= 1.5
                    ) >= 2
                ):
                    ended_early = False
                # Ragged right: the line ended short only because the next
                # word did not fit in what was left ("... Road to" /
                # "Gulshan-e-Ravi ..." was split into two paragraphs).
                # Only mid-sentence (ending in a word or a comma): a list of
                # specifications ("...24/25/30p)" / "3840x2160 ...") ends each
                # line where its item does.
                if (
                    ended_early and re.search(r"[A-Za-z,&\-]$", previous.text.rstrip())
                    and re.match(r"[A-Za-z(][^\s:]*(?:\s|$)", line.text.lstrip()) is not None
                    and _first_word_width(line) > edge - previous.bbox[2] - 0.3 * size
                ):
                    ended_early = False
                new = indented or ended_early
            # A line closing a bracket the paragraph left open continues it
            # ("... with 3 Standby" / "Pumps).", set indented).
            if new and _closes_open_bracket(current, line) and abs(line.size - size) <= 0.6 and line.color == previous.color:
                new = False
            if new:
                paragraphs.append(_Paragraph(page_number, current, current_centred))
                current = []
                # A full-width line only counts as centred while it continues
                # a centred block; starting a new paragraph after a centred
                # title it is body text ("Contractor shall submit..." was
                # centred, and its last line split off as a paragraph).
                centred = own_centred
        if not current:
            current_centred = centred
        current.append(line)
    if current:
        paragraphs.append(_Paragraph(page_number, current, current_centred))
    return paragraphs


# "1.", "a)", "(iv)", "viii.", "[2]", or a bullet character.
_LIST_LABEL_RE = re.compile(r"^(?:[(\[]?(?:\d{1,3}(?:\.\d{1,3})*|[ivxlcdm]{1,6}|[IVXLCDM]{1,6}|[A-Za-z])[.)\]]|[•·▪●○■□◆◇\-–—])$")
_CLAUSE_NUMBER_RE = re.compile(r"\d{1,3}(?:\.\d{1,3})+")


def _label_text_start(first: _VisualLine) -> float | None:
    """Where the text starts after a short leading label ("viii.", "(a)", "1.")."""
    chars = sorted(
        (char for span in first.spans for char in span.get("chars", ())),
        key=lambda char: char["bbox"][0],
    )
    label: list[str] = []
    previous_end = None
    for char in chars:
        value = str(char.get("c", ""))
        if not value.strip():
            if label:
                label.append(" ")
            continue
        # The label and its text may be separate pieces with no space
        # between them, only a gap ("iii." right-aligned, text at 101pt).
        gap = char["bbox"][0] - previous_end if previous_end is not None else 0.0
        if label and (gap > 0.3 * first.size or (gap > 0.1 * first.size and _LIST_LABEL_RE.match("".join(label)))):
            label.append(" ")
        if label and label[-1] == " ":
            text = "".join(label).strip()
            # A clause number with no closing dot ("23.4  The inner ...") is
            # a label only when set apart by a tab stop, not a word space
            # ("2.5 m deep"): read as plain text, each clause's first line
            # became a paragraph of its own.
            numbered = _CLAUSE_NUMBER_RE.fullmatch(text) is not None and gap > 0.6 * first.size
            return char["bbox"][0] if _LIST_LABEL_RE.match(text) or numbered else None
        label.append(value)
        previous_end = char["bbox"][2]
    return None


# The value may stand at a tab stop after several spaces ("Name of Project:  Lahore").
_KEY_VALUE_RE = re.compile(r"^[A-Za-z][\w./()-]{0,20}(?: [\w./(),-]{1,20}){0,3}:[ \t]+\S")


def _first_word_width(line: _VisualLine) -> float:
    """Width of the first word of ``line``, as drawn (0 if none)."""
    chars = sorted(
        (char for span in line.spans for char in span.get("chars", ())),
        key=lambda char: char["bbox"][0],
    )
    start = end = None
    for char in chars:
        value = str(char.get("c", ""))
        if not value.strip():
            if start is not None:
                break
            continue
        if start is None:
            start = char["bbox"][0]
        end = char["bbox"][2]
        # A Chinese line breaks between any two characters.
        if _CJK_RE.match(value):
            break
    return 0.0 if start is None else end - start


def _closes_open_bracket(current: list[_VisualLine], line: _VisualLine) -> bool:
    text = " ".join(item.text for item in current)
    opened = text.count("(") + text.count("（") - text.count(")") - text.count("）")
    head = line.text
    close = min((i for i in (head.find(")"), head.find("）")) if i >= 0), default=-1)
    open_ = min((i for i in (head.find("("), head.find("（")) if i >= 0), default=len(head))
    return opened > 0 and 0 <= close < open_


def _continues_hanging_item(first: _VisualLine, line: _VisualLine) -> bool:
    """``line`` starts where the text of ``first`` does, after its label or
    its lead-in ("May 17, 2024: Two surveyors ..." / "work.")."""
    # A lead-in's text is followed by a tab stop the next lines hang at,
    # a few points from where this line's text happened to start.
    return any(
        start is not None
        and abs(line.bbox[0] - start) <= tolerance
        and line.bbox[0] > first.bbox[0] + 0.8 * first.size
        for start, tolerance in ((_label_text_start(first), 1.5), (_lead_in_text_start(first), max(1.5, 0.5 * first.size)))
    )


def _lead_in_text_start(first: _VisualLine) -> float | None:
    """Where the text starts after a short lead-in ending in a colon."""
    if not _KEY_VALUE_RE.match(first.text):
        return None
    chars = sorted(
        (char for span in first.spans for char in span.get("chars", ())),
        key=lambda char: char["bbox"][0],
    )
    after_colon = False
    for char in chars:
        value = str(char.get("c", ""))
        if after_colon and value.strip():
            return char["bbox"][0]
        if value == ":":
            after_colon = True
    return None


def _needs_translation(text: str, source_language: str) -> bool:
    if source_language.lower().startswith("zh"):
        return bool(_CJK_RE.search(text))
    # A bare code ("J01-L3C", "R05-A") has nothing to translate; rewriting it
    # only risks changing it.
    from .pdf_pipeline import _IMMUTABLE_IDENTIFIER_RE

    # A column of item letters ("A" / "B" / "C") is a list of labels:
    # translated, it came back as "甲乙丙丁戊".
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and all(re.fullmatch(r"\(?[A-Za-z][.)]?", line) for line in lines):
        return False
    return any(char.isalpha() for char in _IMMUTABLE_IDENTIFIER_RE.sub("", text))


def _unit(
    text: str,
    part: str,
    object_id: str,
    style: str,
    source_hash: str,
    source_language: str,
    target_language: str,
) -> TranslationUnit:
    text, dates = localize_chinese_dates(text, source_language, target_language)
    data = {
        "document_hash": source_hash,
        "format": DocumentFormat.PDF,
        "location": DocumentLocation(part=part, object_id=object_id),
        "source_language": source_language,
        "target_language": target_language,
        "source_text": text,
        "protected_tokens": rule_protected_tokens(text, [*dates, *(m for pair in _MARKS.values() for m in pair if m in text)]),
        "style_signature": style,
        "context_before": "",
        "context_after": "",
    }
    return TranslationUnit(id=generate_unit_id(**data), **data)


_MAX_BATCH_ITEMS = 40

_PAGE_OF_EN = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)
_PAGE_OF_ZH = re.compile(r"第\s*(\d+)\s*页\s*[，,]?\s*共\s*(\d+)\s*页")
# "3 | Page", "Page | 3", "Page 3", the word often letter-spaced ("P a g e").
_PAGE_NUMBER_EN = re.compile(r"(?:(\d+)\s*[|\-–—]?\s*P\s?a\s?g\s?e|P\s?a\s?g\s?e\s*[|\-–—]?\s*(\d+))", re.I)


def _page_footer_translation(unit: TranslationUnit) -> str | None:
    """"Page 2 of 2" footers in one fixed form: translated separately, page 1
    came back as "第 1 页，共 2 页" and page 2 as "2 / 2"."""
    text = unit.source_text.strip()
    if unit.target_language.lower().startswith("zh") and (match := _PAGE_OF_EN.fullmatch(text)):
        return f"第{match.group(1)}页，共{match.group(2)}页"
    # Translated as words, the number was kept and "Page" became "第页".
    if unit.target_language.lower().startswith("zh") and (match := _PAGE_NUMBER_EN.fullmatch(text)):
        return f"第{match.group(1) or match.group(2)}页"
    if unit.target_language.lower().startswith("en") and (match := _PAGE_OF_ZH.fullmatch(text)):
        return f"Page {match.group(1)} of {match.group(2)}"
    return None


def _translate_all(
    provider: TranslationBatchProvider,
    units: list[TranslationUnit],
    *,
    cache: Any | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[dict[str, str], list[dict[str, object]], dict[str, object]]:
    """Translate every unit: each distinct text once, concurrently, cache first.

    * Identical source texts (running headers/footers, repeated table
      headings) are translated once and reused -- the translation of a unit
      depends only on its text and protected tokens (that is also the cache
      key), not on where it sits.
    * Batches are packed to the model's own character budget, so every batch
      is one provider request.
    * Several batches are in flight at once (DOCUMENT_TRANSLATOR_PDF_WORKERS,
      default 6): sequential requests made a 777-page document take hours.
    * With a cache, already-translated texts cost nothing on a rerun.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from document_translator.pdf_worker import _pdf_runtime_limits

    def text_key(unit: TranslationUnit) -> tuple[str, tuple[str, ...]]:
        return unit.source_text, tuple(unit.protected_tokens)

    groups: dict[tuple[str, tuple[str, ...]], list[TranslationUnit]] = {}
    for unit in units:
        groups.setdefault(text_key(unit), []).append(unit)
    representatives = [members[0] for members in groups.values()]
    rep_by_id = {unit.id: unit for unit in representatives}

    identity = {
        "provider": getattr(provider, "provider_name", ""),
        "model": str(getattr(getattr(provider, "config", None), "model", "")),
        "prompt_version": getattr(provider, "prompt_version", ""),
        "glossary_version": getattr(provider, "glossary_version", ""),
    }
    results: dict[str, TranslationResult] = {}
    pending: list[TranslationUnit] = []
    for unit in representatives:
        local = _page_footer_translation(unit)
        if local is not None:
            results[unit.id] = TranslationResult(
                unit_id=unit.id, translation=local, source_hash=sha256_text(unit.source_text),
                result_hash=sha256_text(local), request_count=1, validation_status="valid",
                **{key: value or "local" for key, value in identity.items()},
            )
            continue
        cached = None
        if cache is not None:
            try:
                cached = cache.get(unit, **identity)
            except Exception:  # a stale/corrupt entry is simply re-translated
                cached = None
        if cached is not None:
            results[unit.id] = cached
        else:
            pending.append(unit)

    config = getattr(provider, "config", None)
    from .batch_runner import plan_batches, worker_count

    batches: list[list[TranslationUnit]] = []
    # plan_batches also splits a short document across the workers: packed
    # into one request, a 3-page notice took 56 s on flash.
    for packed in plan_batches(
        pending, identity["model"], int(getattr(config, "batch_input_characters", 0) or 0), worker_count(),
    ):
        for offset in range(0, len(packed), _MAX_BATCH_ITEMS):
            if packed[offset : offset + _MAX_BATCH_ITEMS]:
                batches.append(packed[offset : offset + _MAX_BATCH_ITEMS])

    def store(result: TranslationResult) -> None:
        unit = rep_by_id[result.unit_id]
        results[result.unit_id] = result
        if cache is not None and result.validation_status == "valid" and not validate_result_for_unit(unit, result):
            try:
                cache.put(unit, result)
            except Exception:
                pass

    _, workers = _pdf_runtime_limits()
    done_units = len(results)
    total_units = len(representatives)
    if progress is not None:
        progress(f"translation: {total_units} distinct texts ({len(units)} total), {done_units} from cache, {len(batches)} requests")
    if batches:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(batches)))) as pool:
            futures = {pool.submit(_translate_batch_with_retry, provider, batch): batch for batch in batches}
            try:
                for future in as_completed(futures):
                    batch = futures[future]
                    batch_results = future.result()
                    if len(batch_results) != len(batch):
                        raise RuntimeError("translation provider returned an incomplete batch")
                    for result in batch_results:
                        if result.unit_id not in rep_by_id:
                            raise RuntimeError("translation provider returned an unknown unit ID")
                        store(result)
                    done_units += len(batch)
                    if progress is not None:
                        progress(f"translation: {done_units}/{total_units}")
            except BaseException:
                for future in futures:
                    future.cancel()
                raise

    translations: dict[str, str] = {}
    rep_warnings: list[dict[str, object]] = []
    for unit in representatives:
        result = results[unit.id]
        errors = validate_result_for_unit(unit, result)
        if result.validation_status == "needs_review" and result.error:
            errors = list(dict.fromkeys([*errors, *result.error.split("; ")]))
        if errors:
            rep_warnings.append({"unit_id": unit.id, "object_id": unit.location.object_id, "errors": errors})
        translations[unit.id] = result.translation
    if rep_warnings:
        translations, rep_warnings = _remediate_warnings(provider, rep_by_id, translations, rep_warnings)

    # Every unit takes its representative's translation and warnings.
    warning_by_rep = {entry["unit_id"]: entry for entry in rep_warnings}
    expanded: dict[str, str] = {}
    warnings: list[dict[str, object]] = []
    for members in groups.values():
        rep = members[0]
        for unit in members:
            expanded[unit.id] = translations[rep.id]
            if rep.id in warning_by_rep:
                warnings.append({**warning_by_rep[rep.id], "unit_id": unit.id, "object_id": unit.location.object_id})
    stats = {
        "units": len(units),
        "distinct_texts": total_units,
        "from_cache": total_units - len(pending),
        "requests": len(batches),
        "workers": workers,
    }
    return expanded, warnings, stats


@dataclass(frozen=True)
class _PageWidth:
    """The one page attribute pdf_layout._alignment() reads (page.rect.width)."""

    width: float

    @property
    def rect(self) -> "_PageWidth":
        return self


def _alignment_of(paragraph: _Paragraph, bounds: tuple[float, float]) -> int:
    if paragraph.centred:
        return 1
    block = {"lines": [{"spans": line.spans} for line in paragraph.lines]}
    align = _alignment(_PageWidth(paragraph.page_width), block, bounds, paragraph.size)
    # A list item is set from its left edge, wherever it happens to end.
    first = paragraph.lines[0]
    if align in (1, 2) and (first.bulleted or _label_text_start(first) is not None):
        return 0
    return 0 if align == 2 and paragraph.left_anchored else align


def _normalise_structure(
    paragraph: _Paragraph, translation: str, bounds: tuple[float, float], profile: object, target_language: str
) -> str:
    """Apply the project's Chapter/Article and document-number conventions."""
    if target_language.lower().startswith("en"):
        translation = _bracket_cited_references(paragraph.text, translation)
    role, _ = _role_for_source(paragraph.text, _alignment_of(paragraph, bounds), paragraph.size)
    if role in {"chapter_heading", "article_heading", "article_text"}:
        return normalize_numbered_translation(paragraph.text, translation, profile)
    if role == "document_reference" and target_language.lower().startswith("en"):
        return normalize_document_reference_translation(paragraph.text, translation, profile)
    return translation


_CITED_REFERENCE_RE = re.compile(r"[〔\[［(（](\d{4})[〕\]］)）]\s*(\d+)\s*号")


def _bracket_cited_references(source: str, translation: str) -> str:
    """Document numbers cited in running text ("中土经营〔2024〕341号") keep the
    same "[2024] No. 341" form as a standalone reference line."""
    for year, serial in _CITED_REFERENCE_RE.findall(source):
        translation = re.sub(
            rf"(?:[\[(（〔]\s*)?\b{year}\b\s*[\])）〕]?\s*(?:No\.?\s*)?\b{serial}\b(?:\s*No\.)?",
            f"[{year}] No. {serial}",
            translation,
        )
    return translation


# ---------------------------------------------------------------- rendering


def _latin_font(source_font: str, size: float, page: Any) -> str:
    """Match the source face's character for an English replacement.

    黑体 (Hei) headings read as a bold sans, 宋/标宋 display titles as a bold
    serif, 仿宋/楷 body text as a plain sans -- the same visual weight the
    source used, rather than one font for everything.
    """
    name = source_font.casefold()
    if "hei" in name:
        preferred = r"C:\Windows\Fonts\arialbd.ttf"
    elif any(key in name for key in ("xbs", "song", "sun", "ming", "biaosong")) and "fang" not in name:
        preferred = r"C:\Windows\Fonts\timesbd.ttf" if size >= 18 else r"C:\Windows\Fonts\times.ttf"
    else:
        preferred = r"C:\Windows\Fonts\arial.ttf"
    if Path(preferred).is_file():
        return preferred
    return _font_file(source_font, "A", page=page) or r"C:\Windows\Fonts\arial.ttf"


def _font_alias(fontfile: str) -> str:
    return "IP" + hashlib.sha1(fontfile.encode("utf-8")).hexdigest()[:8]


def _render_paragraphs(
    doc: Any,
    paragraphs: list[_Paragraph],
    translatable: list[_Paragraph],
    units: list[TranslationUnit],
    translations: dict[str, str],
    tables: list[Any],
    flow: dict[int, _Paragraph] | None = None,
) -> dict[str, object]:
    import fitz

    _FRAMES.clear()
    # Plan every paragraph on every page first: the space it may use, and the
    # largest size at which its translation fits there.
    plans: list[tuple[_Paragraph, str, Any]] = []
    for paragraph, unit in zip(translatable, units):
        # A name kept as is ("GULSHAN E RAVI DS"): the original glyphs, in
        # their own bold face and position, are already the right output.
        if " ".join(translations[unit.id].split()) == " ".join(paragraph.text.split()):
            continue
        page = doc[paragraph.page_number - 1]
        page_paragraphs = [p for p in paragraphs if p.page_number == paragraph.page_number]
        own = [fitz.Rect(line.bbox) for line in paragraph.lines]
        others = [
            fitz.Rect(line.bbox) for p in page_paragraphs for line in p.lines
            if not any(fitz.Rect(line.bbox) == o for o in own)
        ]
        others += [fitz.Rect(t.rect) for t in tables if t.page_number == paragraph.page_number]
        # An image the paragraph already sits on (a seal stamped over a
        # signature) is not an obstacle; any other image is.
        others += [
            fitz.Rect(info["bbox"]) for info in page.get_image_info()
            if not fitz.Rect(info["bbox"]).intersects(fitz.Rect(paragraph.bbox))
        ]
        # Chosen now, before any paragraph is written: once SimHei was on the
        # page, the clash check took it for a source font and set the next
        # paragraph in YaHei, so one page mixed two faces (and sizes fitted
        # with SimHei were written in YaHei).
        paragraph.cjk_font = _font_file(paragraph.lines[0].font, "中", page=page) or r"C:\Windows\Fonts\simhei.ttf"
        plans.append((paragraph, translations[unit.id], _region(page, paragraph, others, paragraph.bounds)))
    if flow:
        plans = _flow_over_pages(doc, plans, flow)
    fitted = [
        _fit_size(doc[p.page_number - 1], p, text, region, p.bounds) if text else p.size
        for p, text, region in plans
    ]

    sizes, group_size = _shared_sizes([paragraph for paragraph, _, _ in plans], fitted)

    def size_of(paragraph: _Paragraph) -> float:
        return sizes[id(paragraph)]

    shrunk: list[dict[str, object]] = []
    scanned_pages: list[int] = []
    for page_number in sorted({p.page_number for p, _, _ in plans}):
        page = doc[page_number - 1]
        page_plans = [plan for plan in plans if plan[0].page_number == page_number]
        scanned = _scanned_page(page)
        if scanned:
            scanned_pages.append(page_number)
        # Remove every original glyph being replaced first, then write.
        for paragraph, _, _ in page_plans:
            # A small box at each glyph's centre: a whole-span box also
            # removed glyphs of neighbouring labels whose (tall, often
            # rotated) boxes merely touched it.
            for span in (s for line in paragraph.lines for s in line.spans):
                for x0, y0, x1, y1 in _glyph_centre_bands(span):
                    page.add_redact_annot(fitz.Rect(x0, y0, x1, y1), fill=False)
        page.apply_redactions(images=0, graphics=0, text=0)
        rules = _rule_drawings(page)
        if scanned:
            # The words are pixels of the scan, under an invisible OCR layer:
            # written over them, the translation and the English were both
            # unreadable. The scanned words, and the space each translation
            # will take, are covered -- all of them before any is written,
            # so one cover never hides another paragraph's new text.
            for paragraph, text, region in page_plans:
                if text:
                    cover = fitz.Rect(paragraph.bbox) | _written_extent(page, paragraph, text, region, size_of(paragraph))
                    page.draw_rect(cover + (-1, -1, 1, 1), color=None, fill=(1, 1, 1), overlay=True)
        for paragraph, text, region in page_plans:
            if not text:
                continue  # its text now ends on the previous page
            if paragraph.toc_page is not None:
                _insert_toc_entry(page, paragraph, text, size_of(paragraph))
                continue
            hanging = _hanging_label(paragraph, text)
            if hanging is not None:
                label, text, text_start = hanging
                # A translated lead-in may be wider than the source's.
                from .pdf_table import cached_font

                label_font = paragraph.cjk_font if (_CJK_RE.search(label) and paragraph.cjk_font) else _latin_font(paragraph.lines[0].font, paragraph.size, page)
                label_end = paragraph.lines[0].bbox[0] + cached_font(label_font).text_length(label, fontsize=size_of(paragraph))
                text_start = max(text_start, label_end + 0.25 * paragraph.size)
                region = fitz.Rect(text_start, region.y0, region.x1, region.y1)
            size, written_box = _insert(page, paragraph, text, region, size_of(paragraph), paragraph.bounds)
            if hanging is not None:
                _insert_label(page, paragraph, label, size, written_box)
            _refit_underline(page, paragraph, text, size, written_box, rules)
            if size < paragraph.size - 1e-6:
                shrunk.append({"page": page_number, "source_size": round(paragraph.size, 2), "size": size, "text": text[:60]})
    styles: dict[tuple[object, ...], float] = {}
    for key, size in group_size.items():
        styles[key[1:]] = min(styles.get(key[1:], size), size)
    return {
        "paragraphs": len(translatable),
        # the smallest size each style was set at on any page
        "style_sizes": [{"source_size": key[0], "font": key[1], "size": size} for key, size in styles.items()],
        "size_reduced": shrunk,
        **({"scanned_pages_covered": scanned_pages} if scanned_pages else {}),
    }


def _shared_sizes(paragraphs: list[_Paragraph], fitted: list[float]) -> tuple[dict[int, float], dict[tuple[object, ...], float]]:
    """The size each paragraph is written at (by id), and each (page, style) group's.

    Paragraphs of a page sharing a source style (body text, chapter
    headings, ...) share one size, as the source does: the smallest any of
    them needed, so every paragraph still fits its own place. Sizing each on
    its own gave a page of body text in five different sizes. Shared over
    the whole document, one cramped paragraph set 57 pages of a tender at
    6pt instead of 10pt; one that fits only far below its source size keeps
    that size for itself, as an outlier table cell does.
    """
    group_size: dict[tuple[object, ...], float] = {}
    own_size: dict[int, float] = {}
    for paragraph, size in zip(paragraphs, fitted):
        if size < 0.75 * paragraph.size:
            own_size[id(paragraph)] = size
            continue
        key = (paragraph.page_number, *_style_key(paragraph))
        group_size[key] = min(group_size.get(key, size), size)
    sizes = {
        id(paragraph): own_size.get(id(paragraph)) or group_size[(paragraph.page_number, *_style_key(paragraph))]
        for paragraph in paragraphs
    }
    return sizes, group_size


# "4. Reference Datum ........ 6": a title, a dot leader and a page number,
# which may carry its section ("Scope of Tender ........ 1-3").
_TOC_ENTRY_RE = re.compile(r"^(?P<title>.*?\S)\s*(?:[.·…]\s?){4,}\s*(?P<page>(?:\d{1,3}\s?[-–]\s?)?\d{1,4})\s*$")


def _insert_toc_entry(page: Any, paragraph: _Paragraph, title: str, size: float) -> None:
    """Translated title, then dots up to the page number, which ends where
    the source's did (the dots, sent to translation and reflowed, put the
    numbers anywhere and wrapped entries onto two lines)."""
    import fitz

    from .pdf_table import cached_font

    first = paragraph.lines[0]
    title = re.sub(r"\s*(?:[.·…]\s?){3,}\s*(?:\d{1,3}\s?[-–]\s?)?\d{0,4}\s*$", "", title.strip())
    label_start = _label_text_start(first)
    label = ""
    if label_start is not None:
        label = "".join(
            str(char.get("c", "")) for span in first.spans for char in span.get("chars", ())
            if char["bbox"][2] <= label_start + 0.5
        ).strip()
        if title.startswith(label):
            title = title[len(label):].strip()
        else:
            label, label_start = "", None
    fontfile = paragraph.cjk_font if (_CJK_RE.search(title) and paragraph.cjk_font) else _latin_font(first.font, size, page)
    title = _with_font_glyphs(title, cached_font(fontfile))
    number = paragraph.toc_page or ""
    x0, right = (label_start if label_start is not None else first.bbox[0]), first.bbox[2]
    font = cached_font(fontfile)
    while True:
        dots = right - x0 - font.text_length(f"{title}  {number}", fontsize=size)
        count = int(dots / max(font.text_length(".", fontsize=size), 0.1))
        if count >= 3 or size <= 0.6 * paragraph.size:
            break
        size = round(size - _FONT_STEP, 2)
    content = f"{title} {'.' * max(count, 3)} "
    color = _rgb(first.color)
    bold = {"render_mode": 2, "fill": color, "border_width": 0.04} if (_is_bold(paragraph) and fontfile == paragraph.cjk_font) else {}
    alias = _font_alias(fontfile)
    if label:
        _insert_label(page, paragraph, label, size, fitz.Rect(first.bbox))
    page.insert_text((x0, first.baseline), content, fontsize=size, fontfile=fontfile, fontname=alias, color=color, overlay=True, **bold)
    number_x = right - font.text_length(number, fontsize=size)
    page.insert_text((number_x, first.baseline), number, fontsize=size, fontfile=fontfile, fontname=alias, color=color, overlay=True, **bold)


def _hanging_label(paragraph: _Paragraph, text: str) -> tuple[str, str, float] | None:
    """(label, rest of the text, x where the text starts) for a numbered
    item whose wrapped lines hang under its text ("3. Updated working on
    ..." / "   the assignment within ..."), if the translation keeps the label."""
    # A one-line item too: its text starts where the other items' text
    # does, not right after its own (narrower or wider) label.
    first = paragraph.lines[0]
    text_start = _label_text_start(first)
    if text_start is None:
        # A lead-in ("May 17, 2024:"): translated, so split at its colon.
        lead = _lead_in_text_start(first)
        colon = min((i for i in (text.find("："), text.find(":")) if 0 < i <= 30), default=-1)
        if lead is None or colon < 0 or len(paragraph.lines) < 2 or not all(
            abs(line.bbox[0] - lead) <= max(1.5, 0.5 * first.size) for line in paragraph.lines[1:]
        ):
            return None
        return text[: colon + 1].strip(), text[colon + 1:].strip(), paragraph.lines[1].bbox[0]
    if not all(abs(line.bbox[0] - text_start) <= 1.5 for line in paragraph.lines[1:]):
        return None
    # The label is what stands before the text start ("3." of "3.Updated").
    label = "".join(
        str(char.get("c", "")) for span in first.spans for char in span.get("chars", ())
        if char["bbox"][2] <= text_start + 0.5
    ).strip()
    stripped = text.lstrip()
    if not label or not stripped.startswith(label):
        return None
    return label, stripped[len(label):].lstrip(), text_start


def _insert_label(page: Any, paragraph: _Paragraph, label: str, size: float, written_box: Any) -> None:
    """The label on the baseline its text was actually written on, in the
    same face (on the source baseline it sat lower than a CJK heading)."""
    first = paragraph.lines[0]
    baseline, font_name = first.baseline, ""
    for block in page.get_text("dict", clip=written_box).get("blocks", ()):
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                if str(span.get("text", "")).strip():
                    baseline, font_name = float(span["origin"][1]), str(span.get("font", ""))
                    break
            if font_name:
                break
        if font_name:
            break
    from .pdf_table import cached_font

    # "viii." in a Latin face (SimHei spaced it out as "v i i i ."); a bullet
    # in the text's face, as a glyph that face has.
    fontfile = _latin_font(first.font, size, page) if label.isascii() or not paragraph.cjk_font else paragraph.cjk_font
    if fontfile != paragraph.cjk_font and _is_bold(paragraph):
        bold = Path(fontfile).with_name(Path(fontfile).stem.rstrip("bd") + "bd" + Path(fontfile).suffix)
        fontfile = str(bold) if bold.is_file() else fontfile
    label = _with_font_glyphs(label, cached_font(fontfile))
    # The label's own colour ("viii." in blue before black text).
    label_span = next(
        (span for span in first.spans if str(span.get("text", "")).strip()),
        None,
    )
    label_color = int(label_span.get("color") or 0) if label_span is not None else first.color
    page.insert_text(
        (first.bbox[0], baseline), label, fontsize=size, fontfile=fontfile,
        fontname=_font_alias(fontfile), color=_rgb(label_color), overlay=True,
        **({"render_mode": 2, "fill": _rgb(label_color), "border_width": 0.04} if _is_bold(paragraph) and fontfile == paragraph.cjk_font else {}),
    )


def _flow_over_pages(doc: Any, plans: list[tuple[Any, str, Any]], flow: dict[int, _Paragraph]) -> list[tuple[Any, str, Any]]:
    """A paragraph broken by a page is set like any paragraph: as much as
    fits in the first page's place, the rest at the top of the next page
    (nothing there when it all fits)."""
    plans = list(plans)
    index_of = {id(paragraph): index for index, (paragraph, _, _) in enumerate(plans)}
    for head_id, tail in flow.items():
        if head_id not in index_of or id(tail) not in index_of:
            continue
        head_index, tail_index = index_of[head_id], index_of[id(tail)]
        head, text, region = plans[head_index]
        page = doc[head.page_number - 1]
        size = round(head.size * 2) / 2 or head.size
        breaks = [i for i in range(1, len(text)) if _can_break(text, i)] + [len(text)]

        def fits(cut: int) -> bool:
            # At the line pitch the text will be written with, so the part on
            # this page keeps the paragraph's spacing.
            content, fontfile, alias, align = _layout(page, head, text[:cut].rstrip(), size, head.bounds, region.width)
            # at the spacing the text will be written with
            return _chinese_pitch(region, content, fontfile, alias, size, align) is not None or (
                not _CJK_RE.search(content) and _fits(region, content, fontfile, alias, size, align)
            )

        low, high, best = 0, len(breaks) - 1, 0
        while low <= high:
            middle = (low + high) // 2
            if fits(breaks[middle]):
                best, low = breaks[middle], middle + 1
            else:
                high = middle - 1
        if best == 0:
            continue  # nothing fits up there: leave it to the normal fitting
        plans[head_index] = (head, text[:best].rstrip(), region)
        plans[tail_index] = (tail, text[best:].strip(), plans[tail_index][2])
    return plans


def _can_break(text: str, index: int) -> bool:
    """A line may end before ``text[index]``: never inside a Latin word or number."""
    from .pdf_table import _NO_LINE_END, _NO_LINE_START

    before, after = text[index - 1], text[index]
    if before.isspace() or after.isspace():
        return True
    # Not before closing punctuation nor after an opening bracket: a row
    # split by a page left "）。" to open the next page's cell.
    if after in _NO_LINE_START or before in _NO_LINE_END:
        return False
    return not (before.isascii() and before.isalnum() and after.isascii() and after.isalnum())


def _glyph_centre_bands(span: dict[str, Any]) -> list[tuple[float, float, float, float]]:
    """Thin boxes through the glyph centres of ``span``: one per level span
    (one annotation per glyph made a 100-page file 4x slower), one per glyph
    when the span is tilted and a single band would miss its ends."""
    chars = [c for c in span.get("chars", ()) if c["bbox"][2] > c["bbox"][0]]
    if not chars:
        return []
    centres = [((c["bbox"][0] + c["bbox"][2]) / 2, (c["bbox"][1] + c["bbox"][3]) / 2) for c in chars]
    height = max(c["bbox"][3] - c["bbox"][1] for c in chars)
    dy = max(height * 0.05, 0.05)
    if max(y for _, y in centres) - min(y for _, y in centres) <= dy:
        cy = sum(y for _, y in centres) / len(centres)
        return [(chars[0]["bbox"][0] + 0.1, cy - dy, chars[-1]["bbox"][2] - 0.1, cy + dy)]
    return [
        (cx - max((c["bbox"][2] - c["bbox"][0]) * 0.1, 0.05), cy - dy, cx + max((c["bbox"][2] - c["bbox"][0]) * 0.1, 0.05), cy + dy)
        for c, (cx, cy) in zip(chars, centres)
    ]


def _style_key(paragraph: _Paragraph) -> tuple[object, ...]:
    first = paragraph.lines[0]
    return (round(paragraph.size * 2) / 2, first.font, first.color, paragraph.centred)


def _region(page: Any, paragraph: _Paragraph, obstacles: list[Any], bounds: tuple[float, float]) -> Any:
    """The space a paragraph's translation may use: its own box, plus free space.

    Starts from the source text's own box and grows only into blank space --
    never over other text, a table, or an unrelated image: to the right for
    left-anchored text, to both margins for centred text, to the left for
    right-anchored text, and downward until the next content.
    """
    import fitz

    left, right = bounds
    x0, y0, x1, y1 = paragraph.bbox
    align = _alignment_of(paragraph, bounds)
    band = fitz.Rect(left - 1, y0, right + 1, y1)
    blockers = [r for r in obstacles if r.intersects(band)]
    max_right = min([r.x0 for r in blockers if r.x0 >= x1 - 0.5] + [right])
    min_left = max([r.x1 for r in blockers if r.x1 <= x0 + 0.5] + [left])
    if align == 1:
        # Grow symmetrically so the text keeps the source's own centre.
        centre = (x0 + x1) / 2
        half = max(min(centre - min_left, max_right - centre), (x1 - x0) / 2)
        x0, x1 = centre - half, centre + half
    elif align == 2:
        x0 = min_left
    else:
        x1 = max(x1, max_right)
    x0, x1 = max(x0, 2.0), min(x1, page.rect.width - 2.0)
    # Text in a drawn frame (a callout "To be entered by the Tenderer ...")
    # stays inside it: grown to the margin, it ran out through the frame.
    frame = _frame_around(page, paragraph.bbox)
    if frame is not None:
        x0, x1 = max(x0, frame.x0 + 1.0), min(x1, frame.x1 - 1.0)
    column = fitz.Rect(x0, y1, x1, page.rect.height)
    below = [r.y0 for r in obstacles if r.intersects(column) and r.y0 >= y1 - 0.5]
    bottom = min(below + [page.rect.height - 24.0] + ([frame.y1] if frame is not None else [])) - 1.0
    # Never reach up into a table (or text) directly above: the table pass
    # later clears its own area and would erase any glyph overlapping it.
    above = [r.y1 + 0.5 for r in obstacles if r.x0 < x1 and r.x1 > x0 and y0 - 1.5 <= r.y1 <= y0 + 0.5]
    top = max(above + [y0 - 1.0])
    return fitz.Rect(x0 - 0.5, top, x1 + 0.5, max(y1 + 1.0, bottom))


_FRAMES: dict[tuple[int, int], list[Any]] = {}


def _frame_around(page: Any, bbox: tuple[float, float, float, float]) -> Any:
    """The smallest stroked rectangle drawn around ``bbox`` (a text box), if any."""
    import fitz

    key = (id(page.parent), page.number)
    if key not in _FRAMES:
        _FRAMES[key] = [
            fitz.Rect(d["rect"]) for d in page.get_drawings()
            if d.get("color") is not None and d.get("rect") is not None
            and any(item[0] == "re" for item in d.get("items", ()))
            and d["rect"].width < 0.9 * page.rect.width and d["rect"].height < 0.5 * page.rect.height
        ]
    inner = fitz.Rect(bbox) + (1, 1, -1, -1)
    around = [rect for rect in _FRAMES[key] if rect.contains(inner)]
    return min(around, key=lambda rect: rect.get_area()) if around else None


def _layout(
    page: Any, paragraph: _Paragraph, text: str, size: float, bounds: tuple[float, float], width: float | None = None
) -> tuple[str, str, str, int]:
    """(content, fontfile, alias, align) for writing ``text`` at ``size``.

    English uses its own normal line spacing throughout: the source's wide
    Chinese line pitch (about 1.8x the font size) reads as double-spaced in
    English, and applying it only where it happened to fit gave one page
    two different spacings.
    """
    import fitz

    first = paragraph.lines[0]
    if _CJK_RE.search(text):
        fontfile = paragraph.cjk_font or _font_file(first.font, text, page=page) or r"C:\Windows\Fonts\simhei.ttf"
    else:
        fontfile = _latin_font(first.font, paragraph.size, page)
    align = _alignment_of(paragraph, bounds)
    multi_line = len(paragraph.lines) > 1
    if align == 0 and multi_line:
        align = 3  # the source body text is justified
    indent = first.bbox[0] - paragraph.bbox[0] if multi_line and align in (0, 3) else 0.0
    spaces = 0
    from .pdf_table import _wrap_atomic_phrases, cached_font

    font = cached_font(fontfile)
    cjk = bool(_CJK_RE.search(text))
    # CJK fonts such as SimHei have no NBSP glyph (it shows as a box); use
    # the ideographic space there.
    pad = "\u3000" if cjk or not font.has_glyph(0xA0) else "\u00a0"
    text = _with_font_glyphs(text, font)
    if indent > 0.8 * size:
        spaces = int(round(indent / max(font.text_length(pad, fontsize=size), 0.1)))
    content = pad * spaces + text if spaces else text
    if cjk and width:
        # insert_textbox() only breaks at spaces, so an unspaced Chinese
        # paragraph was cut wherever the width ran out -- inside "IG-541" or
        # an English word. Break it ourselves, between CJK characters only.
        content = _wrap_atomic_phrases(content, fontfile=fontfile, fontname=_font_alias(fontfile), fontsize=size, max_width=width - 1.0)
    return content, fontfile, _font_alias(fontfile), align


# Chinese punctuation with no compatibility form, for a font without it
# ("PP-142、PP-147" in Arial showed a box).
_PUNCTUATION_FALLBACK = {
    "•": "·", "▪": "·", "◾": "·", "●": "·", "、": ", ", "。": ". ", "《": "“", "》": "”", "「": "“", "」": "”",
    "『": "‘", "』": "’", "【": "[", "】": "]", "〔": "[", "〕": "]", "…": "...", "—": "-",
}


def _with_font_glyphs(text: str, font: Any) -> str:
    """A character the font cannot draw becomes its standard equivalent.

    A missing glyph is drawn as a box: "PP-142□PP-147" (a Chinese comma in
    an English line set in Arial), "404km□" (SimHei has no "²"), "□ Central
    Drain" (nor the bullet "•"). The compatibility form ("," and "2") is
    used instead, and the middle dot for a bullet.
    """
    import unicodedata

    result = []
    for char in text:
        if char.isspace() or font.has_glyph(ord(char)):
            result.append(char)
            continue
        alternative = _PUNCTUATION_FALLBACK.get(char) or unicodedata.normalize("NFKC", char)
        drawable = alternative and alternative != char and all(font.has_glyph(ord(c)) for c in alternative)
        result.append(alternative if drawable else char)
    return "".join(result)


def _on_source_baseline(region: Any, paragraph: _Paragraph, fontfile: str, size: float) -> Any:
    """Move the box down so the first line sits on the source's own baseline.

    The source's span boxes include the font's full ascent, so writing from
    their top put every translation a few points above the original line
    (on a map, 5 pt above the label it replaced). Never moves the box up.
    """
    import fitz

    from .pdf_table import cached_font

    span = paragraph.lines[0].spans[0]
    origin = span.get("origin") or (span.get("chars") or [{}])[0].get("origin")
    if not origin:
        return region
    top = float(origin[1]) - cached_font(fontfile).ascender * size
    if top <= region.y0:
        return region
    return fitz.Rect(region.x0, min(top, region.y1 - size), region.x1, region.y1)


def _fit_size(page: Any, paragraph: _Paragraph, text: str, region: Any, bounds: tuple[float, float]) -> float:
    """The source size if the text fits its place; otherwise stepped down until it does."""
    size = round(paragraph.size * 2) / 2 or paragraph.size
    while size - _FONT_STEP >= _ABSOLUTE_MIN_SIZE:
        content, fontfile, alias, align = _layout(page, paragraph, text, size, bounds, region.width)
        # Sitting on the source baseline is preferred, never worth a smaller size.
        if _fits(region, content, fontfile, alias, size, align):
            return size
        size = round(size - _FONT_STEP, 2)
    return size


def _insert(page: Any, paragraph: _Paragraph, text: str, region: Any, size: float, bounds: tuple[float, float]) -> tuple[float, Any]:
    """Write the translation, stepping down further if the real page disagrees.

    insert_textbox() writes NOTHING (silently) when the text overflows, so a
    paragraph must never be handed to it unverified -- one that did simply
    vanished from the output (observed: a cover page's "AIIB Project No."
    line). Returns the size actually written.
    """
    while True:
        content, fontfile, alias, align = _layout(page, paragraph, text, size, bounds, region.width)
        anchored = _on_source_baseline(region, paragraph, fontfile, size)
        box = anchored if _fits(anchored, content, fontfile, alias, size, align) else region
        # Chinese is set 1.5 lines apart where it fits, closer only where
        # the space does not allow it (at the font's own spacing a body read
        # cramped).
        pitch = _chinese_pitch(box, content, fontfile, alias, size, align) if len(paragraph.lines) > 1 or "\n" in content else None
        color = _rgb(paragraph.lines[0].color)
        # A bold source heading stays bold: CJK faces here have no bold
        # file, so the glyphs are filled and outlined in the same colour.
        bold = _CJK_RE.search(content) and _is_bold(paragraph)
        from .pdf_table import _with_reserve

        written = page.insert_textbox(
            _with_reserve(box, content, fontfile, alias, size, pitch, align), content, fontname=alias, fontfile=fontfile, fontsize=size,
            color=color, align=align, overlay=True, lineheight=pitch,
            **({"render_mode": 2, "fill": color, "border_width": 0.04} if bold else {}),
        )
        if written >= 0 or size - _FONT_STEP < _ABSOLUTE_MIN_SIZE:
            return size, box
        size = round(size - _FONT_STEP, 2)


def _written_extent(page: Any, paragraph: _Paragraph, text: str, region: Any, size: float) -> Any:
    """Where ``text`` will be written, found by writing it on a blank page."""
    import fitz

    with fitz.open() as scratch:
        blank = scratch.new_page(width=page.rect.width, height=page.rect.height)
        _insert(blank, paragraph, text, region, size, paragraph.bounds)
        extent = fitz.Rect()
        for block in blank.get_text("dict").get("blocks", ()):
            extent |= fitz.Rect(block["bbox"])
    return extent


def _scanned_page(page: Any) -> bool:
    """A scanned page: a full-page image whose only text is an invisible
    OCR layer (text render mode 3)."""
    import fitz

    trace = page.get_texttrace()
    if not trace or any(span.get("type") != 3 for span in trace):
        return False
    area = page.rect.get_area()
    return any(
        fitz.Rect(rect).get_area() > 0.5 * area
        for image in page.get_images() for rect in page.get_image_rects(image[0])
    )


def _is_bold(paragraph: _Paragraph) -> bool:
    # Weighed by letters, not spans: a bold lead-in ("Compliance
    # Monitoring" + ";") is two spans against one long plain one, and the
    # whole paragraph was set bold.
    spans = paragraph.lines[0].spans
    weight = [max(1, sum(1 for char in str(span.get("text", "")) if char.isalnum())) for span in spans]
    bold = [bool(int(span.get("flags") or 0) & 16) or "bold" in str(span.get("font", "")).casefold() for span in spans]
    return sum(w for w, b in zip(weight, bold) if b) * 2 > sum(weight)


def _rule_drawings(page: Any) -> list[tuple[Any, tuple[float, ...] | None, float]]:
    """Thin horizontal rules (rect, colour, thickness) still on the page."""
    import fitz

    rules = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect is not None and rect.height <= 3.0 and rect.width > 2.0 * max(rect.height, 1.0):
            color = drawing.get("color") or drawing.get("fill")
            rules.append((fitz.Rect(rect), tuple(color) if color else None, max(rect.height, float(drawing.get("width") or 0.0), 0.5)))
    return rules


def _refit_underline(page: Any, paragraph: _Paragraph, text: str, size: float, box: Any, rules: list) -> None:
    """Fit the underlines under a rewritten paragraph to its new text.

    A heading's single underline is redrawn at the translated line's width
    (under a shorter Chinese title it ran on well past the text). Underlines
    of a few words inside running text ("January 29, 2026 (Thursday)") no
    longer mark anything once the text reflows, and were left crossing the
    new lines: those are removed.
    """
    import fitz

    from .pdf_table import cached_font

    # A table's border right under a line of text ("... will be used:" just
    # above a table) is no underline: its ends meet the grid's vertical
    # rules. Taken for one, it was erased with the line's old underlines.
    verticals = [
        fitz.Rect(drawing["rect"]) for drawing in page.get_drawings()
        if drawing.get("rect") is not None and drawing["rect"].width <= 2.0 and drawing["rect"].height > 3.0
    ]

    def grid_line(rect: Any) -> bool:
        return any(
            min(abs(v.x0 - rect.x0), abs(v.x1 - rect.x0), abs(v.x0 - rect.x1), abs(v.x1 - rect.x1)) <= 1.5
            and v.y0 - 1.0 <= rect.y1 and rect.y0 <= v.y1 + 1.0
            for v in verticals
        )

    def under_line(line: _VisualLine) -> list:
        x0, _, x1, y1 = line.bbox
        return [
            rule for rule in rules
            if y1 - 0.4 * line.size <= rule[0].y0 <= y1 + 0.6 * line.size
            and rule[0].x0 >= x0 - 2.0 and rule[0].x1 <= x1 + 2.0
            and not grid_line(rule[0])
        ]

    def remove(rule_rects: list) -> None:
        # Only line art fully inside each box goes, never text.
        for rect in rule_rects:
            page.add_redact_annot(fitz.Rect(rect.x0 - 0.5, rect.y0 - 0.5, rect.x1 + 0.5, rect.y1 + 0.5), fill=False)
        if rule_rects:
            page.apply_redactions(images=0, graphics=1, text=1)

    line = paragraph.lines[-1]
    width = line.bbox[2] - line.bbox[0]
    heading = [rule for rule in under_line(line) if rule[0].width >= 0.8 * width]
    if len(paragraph.lines) == 1 and len(heading) == 1:
        content, fontfile, _, align = _layout(page, paragraph, text, size, paragraph.bounds, box.width)
        text_width = cached_font(fontfile).text_length(content, fontsize=size)
        if "\n" not in content.strip() and text_width <= box.width:
            if align == 1:
                centre = (box.x0 + box.x1) / 2
                left, right = centre - text_width / 2, centre + text_width / 2
            elif align == 2:
                left, right = box.x1 - text_width, box.x1
            else:
                left, right = box.x0, box.x0 + text_width
            rect, color, thickness = heading[0]
            remove([rect])
            y = (rect.y0 + rect.y1) / 2
            page.draw_line((left, y), (right, y), color=color or (0, 0, 0), width=thickness)
            return
    inline = [rule[0] for each in paragraph.lines for rule in under_line(each) if rule[0].width < 0.8 * (each.bbox[2] - each.bbox[0])]
    remove(inline)


def _fits(region: Any, text: str, fontfile: str, alias: str, size: float, align: int, lineheight: float | None = None) -> bool:
    from .pdf_table import probe_textbox

    return probe_textbox(region.width, region.height, text, fontfile=fontfile, fontname=alias, fontsize=size, align=align, lineheight=lineheight)[0] >= 0


def _chinese_pitch(region: Any, content: str, fontfile: str, alias: str, size: float, align: int) -> float | None:
    from .pdf_table import chinese_line_height

    return chinese_line_height(region.width, region.height, content, fontfile=fontfile, fontname=alias, fontsize=size, align=align)


def _render_tables(
    source: Path,
    staged: Path,
    output: Path,
    tables: list[Any],
    cell_translations: dict[str, str],
    source_language: str,
    target_language: str,
    warnings: list[dict[str, object]],
    *,
    joined_rows: dict[str, tuple[Any, Any, str]] | None = None,
    cleared_tails: list[Any] | None = None,
) -> dict[str, object]:
    """Replace each table's cell text in place; the ruled grid is never touched.

    One font size per table page -- the source's own size if every cell fits
    its ORIGINAL cell, otherwise stepped down until every cell does -- with
    no word ever split across lines, each column aligned as in the source.
    A table whose translation fails validation is left exactly as the source
    has it.
    """
    import shutil

    import fitz

    from . import pdf_table

    if not tables:
        shutil.copyfile(staged, output)
        return {"status": "skipped", "reason": "no ruled tables"}
    good: list[Any] = []
    for table in tables:
        mapping = {cell.id: (cell_translations.get(cell.id, cell.text) if not cell.is_empty else "") for cell in table.cells}
        try:
            pdf_table.validate_pdf_table_translations((table,), mapping, source_language=source_language, target_language=target_language)
        except pdf_table.PdfTableError as exc:
            # One cell's translation dropping a name ("Poonch Road.") left
            # the whole table in English. A cell-level problem is reported
            # and only that cell is checked again: a name finding keeps the
            # translation (as for paragraphs), anything else keeps that
            # cell's source text; the table is written.
            warnings.append({"table": f"page:{table.page_number}:table:{table.table_number}", "errors": [str(exc)]})
            for cell in table.cells:
                if cell.is_empty:
                    continue
                try:
                    pdf_table.validate_pdf_table_translations(
                        (dataclasses.replace(table, cells=(cell,)),), {cell.id: mapping[cell.id]},
                        source_language=source_language, target_language=target_language,
                    )
                except pdf_table.PdfTableError as cell_exc:
                    if "NAME" not in str(cell_exc) and "name" not in str(cell_exc):
                        cell_translations[cell.id] = cell.text
            try:
                remapped = {cell.id: (cell_translations.get(cell.id, cell.text) if not cell.is_empty else "") for cell in table.cells}
                for cell in table.cells:
                    if not cell.is_empty and not remapped[cell.id].strip():
                        raise pdf_table.PdfTableMappingError(f"non-empty source cell {cell.id} has an empty translation")
            except pdf_table.PdfTableError:
                continue
        good.append(table)
    pages: dict[int, dict[str, object]] = {}
    sized: list[Any] = []
    alignment: dict[str, tuple[int, bool]] = {}
    with fitz.open(source) as source_doc:
        for page_number in sorted({t.page_number for t in good}):
            page_tables = [t for t in good if t.page_number == page_number]
            alignment.update(_table_alignment(source_doc[page_number - 1], page_tables))
            for head, tail, joined in (joined_rows or {}).values():
                if head.page_number != page_number:
                    continue
                # The size the rest of this page's tables need, then as much
                # of the row as fits the first cell at that size.
                # The page's size depends on what the cell holds, and what
                # it holds on the size: settle both. Filled for a size the
                # table was not set at, the cell was written at another
                # spacing and ended with lines to spare ("此类小").
                size_now, _ = _table_font_size(page_tables, {**cell_translations, head.id: ""})
                filled = None
                for _ in range(4):
                    filled = _fill_continued(head, joined, size_now)
                    if filled is None:
                        break
                    settled, _ = _table_font_size(page_tables, {**cell_translations, head.id: filled[0]})
                    if settled == size_now:
                        break
                    size_now = settled
                if filled is None:
                    filled = _split_continued(joined, len(head.text) / max(1, len(head.text) + len(tail.text)))
                cell_translations[head.id], rest = filled
                if rest:
                    cell_translations[tail.id] = rest
                elif cleared_tails is not None:
                    # All of it is on the first page: the next page's part
                    # of the row is cleared once the tables are written.
                    cell_translations[tail.id] = tail.text
                    cleared_tails.append(tail)
            size, outliers = _table_font_size(page_tables, cell_translations)
            sized.extend(
                dataclasses.replace(t, cells=tuple(dataclasses.replace(c, source_font_size=outliers.get(c.id, size)) for c in t.cells))
                for t in page_tables
            )
            pages[page_number] = {"font_size": size, "tables": len(page_tables)}
            if outliers:
                pages[page_number]["smaller_cells"] = outliers
    if not sized:
        shutil.copyfile(staged, output)
    else:
        # A cell with nothing to translate (a bare number, a code) keeps
        # its own source text.
        mapping = {
            c.id: ("" if c.is_empty else (cell_translations.get(c.id) or c.text))
            for t in sized for c in t.cells
        }
        # A character the cell's font cannot draw, as in paragraphs; a cell
        # left as the source has it is not touched.
        for t in sized:
            for c in t.cells:
                text = mapping[c.id]
                if text and text != c.text:
                    mapping[c.id] = _with_font_glyphs(text, pdf_table.cached_font(str(_cell_font_for(c, text))))
        sizes = [float(page["font_size"]) for page in pages.values()]
        sizes += [float(v) for page in pages.values() for v in dict(page.get("smaller_cells", {})).values()]
        # Every table page in one pass: each cell keeps its page's size
        # (fixed_cell_size); rewriting the whole PDF once per table page made
        # a 777-page file take hours.
        pdf_table.render_table_translations(
            staged,
            output,
            mapping,
            tables=sized,
            fontfile=lambda cell, text: _cell_font_for(cell, text),
            minimum_font_size=min(sizes),
            initial_font_size=max(sizes),
            align=lambda cell: alignment.get(cell.id, (0, False))[0],
            middle_aligned=lambda cell: alignment.get(cell.id, (0, False))[1],
            spread_lines=False,
            fixed_cell_size=True,
            keep_unchanged=True,
            keep_line_breaks=True,
        )
    return {
        "status": "patched" if len(good) == len(tables) else "partially_patched",
        "table_count": len(good),
        "skipped_table_count": len(tables) - len(good),
        "pages": pages,
    }


def _table_alignment(source_page: Any, tables: list[Any]) -> dict[str, tuple[int, bool]]:
    """Column-wise alignment as the source has it; the header row keeps its own."""
    detected = {c.id: _source_cell_alignment(source_page, c) for t in tables for c in t.cells if c.rect is not None}
    alignment: dict[str, tuple[int, bool]] = {}
    for table in tables:
        # The header runs to the first row with cells of its own: above a
        # risk register's column headers sits a full-width title row, and
        # taking that as the header forced the column headers into the
        # body's alignment.
        real_by_row: dict[int, int] = {}
        for c in table.cells:
            if c.rect is not None:
                real_by_row[c.row] = real_by_row.get(c.row, 0) + 1
        header_row = next((row for row in sorted(real_by_row) if real_by_row[row] > 1), min(real_by_row, default=1))
        header_rows = {row for row in real_by_row if row <= header_row}
        for column in {c.column for c in table.cells}:
            body = [detected[c.id] for c in table.cells if c.column == column and c.row not in header_rows and c.id in detected]
            # The column's most common alignment: centred over half of it
            # as before, right-aligned when most of it is, else left.
            centred_count = sum(1 for h, _ in body if h == 1)
            right_count = sum(1 for h, _ in body if h == 2)
            majority = (
                1 if centred_count * 2 > len(body) else 2 if right_count * 2 > len(body) else 0,
                sum(1 for _, m in body if m) * 2 > len(body),
            ) if body else None
            for cell in table.cells:
                if cell.column == column and cell.id in detected:
                    if majority is None:
                        alignment[cell.id] = detected[cell.id]
                    elif (
                        cell.row in header_rows and not detected[cell.id][1]
                        # a heading is short; a page whose table goes on from
                        # the last page starts with a body row instead
                        and len(cell.text) <= 60 and len(cell.text.splitlines()) <= 3
                        and _balanced_in_cell(source_page, cell)
                    ):
                        # A header whose lines fill the cell with equal room
                        # above and below ("Structures need to / Dismantle"):
                        # its shorter translation goes in the middle.
                        alignment[cell.id] = (detected[cell.id][0] if not _fills_cell(source_page, cell) else majority[0], True)
                    elif cell.row in header_rows:
                        # A heading filling its cell reads as left-aligned
                        # whatever it was ("Pipe Diameter (mm)" fills 89%):
                        # then it is set like its column.
                        own = detected[cell.id]
                        ambiguous = own[0] == 0 and majority[0] != 0 and _fills_cell(source_page, cell)
                        alignment[cell.id] = (majority[0], own[1]) if ambiguous else own
                    else:
                        alignment[cell.id] = majority
    return alignment


def _balanced_in_cell(page: Any, cell: Any) -> bool:
    import fitz

    rect = fitz.Rect(cell.rect)
    text = fitz.Rect()
    for block in page.get_text("dict", clip=rect).get("blocks", ()):
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                if str(span.get("text", "")).strip():
                    text |= fitz.Rect(span["bbox"])
    if text.is_empty:
        return False
    return abs((text.y0 - rect.y0) - (rect.y1 - text.y1)) <= max(2.0, 0.1 * rect.height)


def _fills_cell(page: Any, cell: Any) -> bool:
    """The cell's text spans most of its width, so it reads as neither
    left-aligned nor centred."""
    import fitz

    rect = fitz.Rect(cell.rect)
    text = fitz.Rect()
    for block in page.get_text("dict", clip=rect).get("blocks", ()):
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                if str(span.get("text", "")).strip():
                    text |= fitz.Rect(span["bbox"])
    return not text.is_empty and text.width >= 0.8 * rect.width


def _cell_fits(cell: Any, text: str, size: float, spacing: float | None = None) -> bool:
    """``text`` fits ``cell``'s own rectangle at ``size``, no word split."""
    import fitz

    from . import pdf_table

    if cell.rect is None or not text.strip():
        return True
    rect = fitz.Rect(cell.rect)
    # Measured as it is written: a bullet "•" the font lacks is set as "·"
    # before the text is laid out, and stays inline. Measured as "•" each
    # bullet began a line of its own, and a continued cell was filled a line
    # and more short of its border.
    text = _with_font_glyphs(text, pdf_table.cached_font(str(_cell_font_for(cell, text))))
    normalised = pdf_table._normalise_render_text(text, keep_line_breaks=True)
    fontfile = str(_cell_font_for(cell, normalised))
    font = pdf_table.cached_font(fontfile)
    pad = pdf_table._cell_fit_padding(rect, normalised, 2.0)
    width, height = rect.width - 2 * pad, rect.height - 2 * pad
    for paragraph in normalised.split("\n"):
        for atom, _ in pdf_table._tokenize_atoms_with_seps(paragraph):
            if any(font.text_length(word, fontsize=size) > width for word in atom.split(" ")):
                return False
    wrapped = pdf_table._wrap_atomic_phrases(normalised, fontfile=fontfile, fontname="probe", fontsize=size, max_width=width)
    if spacing is not None:
        lines = pdf_table._wrapped_line_count(width, wrapped, fontfile=fontfile, fontname="probe", fontsize=size)
        glyph = (font.ascender - font.descender) * size
        return bool(lines) and (lines - 1) * spacing * size + glyph <= height - 0.5
    if pdf_table.probe_textbox(width, height, wrapped, fontfile=fontfile, fontname="probe", fontsize=size)[0] >= 0:
        return True
    return pdf_table.compact_line_height(width, height, wrapped, fontfile=fontfile, fontname="probe", fontsize=size) is not None


def _fill_continued(head: Any, translation: str, page_size: float | None = None) -> tuple[str, str] | None:
    """A row broken by a page is set like any text: as much as fits in the
    first page's cell, the rest in the next page's (an empty rest: it all
    fits there). Split by the source's proportion, the first cell ended
    half empty mid-sentence ("根据我们的经验，"). None: not even a start fits."""
    # Filled to the brim at its own size the cell did not fit once written
    # (bold, line spacing); filled as if one step larger, it always does.
    size = min(round((head.source_font_size or 10.0) * 2) / 2, page_size or 99.0)
    # At the 1.5 line spacing Chinese is set with: filled at the font's own
    # spacing, the cell then had to be written cramped.
    spacing = 1.5 if _CJK_RE.search(translation) else None
    if _cell_fits(head, translation, size, spacing):
        return translation, ""
    breaks = [i for i in range(1, len(translation)) if _can_break(translation, i)]
    low, high, best = 0, len(breaks) - 1, 0
    while low <= high:
        middle = (low + high) // 2
        if _cell_fits(head, translation[:breaks[middle]].rstrip(), size, spacing):
            best, low = breaks[middle], middle + 1
        else:
            high = middle - 1
    if best == 0:
        return None
    return translation[:best].rstrip(), translation[best:].strip()


def _table_font_size(tables: list[Any], cell_translations: dict[str, str]) -> tuple[float, dict[str, float]]:
    """Largest size (from the source's own) at which every cell fits its original cell."""
    import fitz

    from . import pdf_table

    fonts: dict[str, Any] = {}

    def fits(cell: Any, size: float) -> bool:
        text = "" if cell.is_empty else (cell_translations.get(cell.id) or cell.text)
        return _cell_fits(cell, text, size)

    cells = [c for t in tables for c in t.cells if c.rect is not None]
    size = round(max((c.source_font_size or 10.0) for c in cells) * 2) / 2

    def all_fit(size: float) -> bool:
        # Same answer as testing every cell in order, but the cell that
        # failed last time is tried first: at the next smaller size it is the
        # one most likely to fail again, so most sizes cost one probe.
        for index, cell in enumerate(cells):
            if not fits(cell, size):
                cells.insert(0, cells.pop(index))
                return False
        return True

    start = size
    # A cell that fits only far below the start size (a one-line header cell
    # whose translation is long) gets its own size; sized with it, a whole
    # table dropped from 12pt to 6.5pt. The rest share the largest size at
    # which all of them fit.
    outliers: dict[str, float] = {}
    for cell in list(cells):
        if not fits(cell, round(0.75 * start * 2) / 2):
            own = round(0.75 * start * 2) / 2
            while own - _FONT_STEP >= _ABSOLUTE_MIN_SIZE and not fits(cell, own):
                own = round(own - _FONT_STEP, 2)
            outliers[cell.id] = own
    # Only a handful: when many cells are that tight the table is simply
    # set small, in one size, rather than as a patchwork of sizes.
    if len(outliers) > max(2, 0.05 * len(cells)):
        outliers = {}
    cells = [cell for cell in cells if cell.id not in outliers]
    while size - _FONT_STEP >= _ABSOLUTE_MIN_SIZE and not all_fit(size):
        size = round(size - _FONT_STEP, 2)
    return size, outliers
