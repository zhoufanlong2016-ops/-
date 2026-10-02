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
from document_translator.translation_rules import localize_chinese_dates, rule_protected_tokens

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

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        boxes = [segment.bbox for segment in self.segments]
        return (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))

    @property
    def spans(self) -> list[dict[str, Any]]:
        return [span for segment in self.segments for span in segment.spans]

    @property
    def size(self) -> float:
        return median(float(span.get("size") or 0.0) for span in self.spans)

    @property
    def color(self) -> int:
        return int(self.spans[0].get("color") or 0)

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

    try:
        tables = pdf_table.extract_pdf_tables(source)
    except pdf_table.PdfTableError:
        tables = []
    doc = fitz.open(source)
    try:
        tables = [t for t in tables if not _is_drawing_frame(t, doc[t.page_number - 1])]
        bounds_by_size = _content_bounds(doc, tables)
        paragraphs: list[_Paragraph] = []
        skipped_rotated = 0
        for page_number, page in enumerate(doc, start=1):
            table_rects = [fitz.Rect(t.rect) for t in tables if t.page_number == page_number]
            lines, rotated = _visual_lines(page, table_rects)
            skipped_rotated += rotated
            page_bounds = bounds_by_size[_page_key(page)]
            page_paragraphs = _segment(page_number, lines, page_bounds, _horizontal_rules(page))
            for paragraph in page_paragraphs:
                paragraph.page_width = float(page.rect.width)
                paragraph.bounds = page_bounds
                # Shares its left edge with other text (a legend column, a
                # list): left-anchored even if it happens to reach the right.
                x0 = paragraph.bbox[0]
                paragraph.left_anchored = any(
                    other is not paragraph and not other.centred and abs(other.bbox[0] - x0) <= 1.5
                    for other in page_paragraphs
                )
                paragraphs.append(paragraph)
    finally:
        doc.close()

    translatable = [p for p in paragraphs if _needs_translation(p.text, source_language)]
    paragraph_units = [
        _unit(p.text, f"page:{p.page_number}", f"para:{index}", "paragraph", source_hash, source_language, target_language)
        for index, p in enumerate(translatable)
    ]
    cell_units: list[TranslationUnit] = []
    cell_by_unit: dict[str, Any] = {}
    continued = _continued_cells(tables)
    continuations = set(continued.values())
    for table in tables:
        for cell in table.cells:
            if cell.is_empty or cell.id in continuations:
                continue
            text = cell.text + ("\n" + continued[cell.id].text if cell.id in continued else "")
            if not _needs_translation(text, source_language):
                continue
            # "Drawing No. LW-" / "TD-401" broken across cell lines is one code.
            cell_text = re.sub(r"(?<=[A-Za-z0-9])-\n(?=[A-Za-z0-9])", "-", text)
            unit = _unit(cell_text, f"page:{cell.page_number}", cell.id, "table_cell", source_hash, source_language, target_language)
            cell_units.append(unit)
            cell_by_unit[unit.id] = cell

    translations, warnings, translation_stats = _translate_all(provider, paragraph_units + cell_units, cache=cache, progress=progress)

    with fitz.open(source) as work:
        for paragraph, unit in zip(translatable, paragraph_units):
            paragraph_translation = translations.get(unit.id, "").strip() or unit.source_text
            translations[unit.id] = _normalise_structure(
                paragraph, paragraph_translation, paragraph.bounds, profile, target_language
            )
        rendered = _render_paragraphs(work, paragraphs, translatable, paragraph_units, translations, tables)
        staged = candidate.with_name(candidate.stem + ".paragraphs" + candidate.suffix)
        work.save(str(staged), garbage=1, deflate=True)

    cell_translations: dict[str, str] = {}
    for unit in cell_units:
        cell = cell_by_unit[unit.id]
        translation = translations.get(unit.id, "").strip()
        if cell.id in continued and translation:
            tail = continued[cell.id]
            cell_translations[cell.id], cell_translations[tail.id] = _split_continued(
                translation, len(cell.text) / max(1, len(cell.text) + len(tail.text))
            )
        else:
            cell_translations[cell.id] = _restore_item_breaks(cell.text, translation) if translation else cell.text
    table_report = _render_tables(source, staged, candidate, tables, cell_translations, source_language, target_language, warnings)
    try:
        staged.unlink(missing_ok=True)
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
        "translation_warning_count": len(warnings),
        "translation": translation_stats,
        **({"translation_warnings": warnings} if warnings else {}),
    }


# ---------------------------------------------------------------- structure


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


_ITEM_NUMBER_AT_LINE_START = re.compile(r"(?m)^[ \t]*(\d+(?:\.\d+)+\.?)(?=\s*\S)")


def _restore_item_breaks(source: str, translation: str) -> str:
    """Put each numbered sub-item of a cell back on its own line.

    A milestone cell lists "2.1.1. Inception Report Approved" / "2.1.2
    Liaison..." on separate lines; the model returned one run-on sentence
    ("…获批2.1.2.与利益相关者…"). Only numbers that start a line in the
    source are used, so a reference like "第2.1.1款" is never split.
    """
    numbers = _ITEM_NUMBER_AT_LINE_START.findall(source)
    if len(numbers) < 2:
        return translation
    for number in numbers[1:]:
        translation = re.sub(
            rf"(?<=[^\n])[ \t]*(?=(?<![0-9.]){re.escape(number)}(?![0-9]))", "\n", translation, count=1
        )
    return translation


_SPLIT_MARKS = "。；，、;,. "


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
                span["text"] = "".join(str(char.get("c", "")) for char in span.get("chars", ()))
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
            centre = fitz.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)
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
            segments.append(_Segment(text, tuple(box), spans))
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
            if height > 0 and overlap >= 0.6 * height and gap <= 3.0 * size and not marker_between:
                line.segments.append(segment)
                break
        else:
            lines.append(_VisualLine([segment]))
    lines.sort(key=lambda line: (line.bbox[1], line.bbox[0]))
    return lines, rotated


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
    return any(c.rect is not None and fitz.Rect(c.rect).get_area() > 0.5 * area for c in table.cells)


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
    paragraphs: list[_Paragraph] = []
    current: list[_VisualLine] = []
    current_centred = False
    for line in lines:
        centred = own_centred = _is_centred(line, bounds)
        # The widest line of a centred block is what sets the margins, so it
        # has no open margins of its own; it must not split that block.
        if not centred and current and current_centred and (line.bbox[2] - line.bbox[0]) >= 0.95 * (right - left):
            centred = True
        if current:
            previous = current[-1]
            size = previous.size
            new = (
                abs(line.size - size) > 0.6
                or line.color != previous.color
                or line.bbox[1] - previous.bbox[1] > 2.0 * size
                or line.bbox[1] < previous.bbox[1]
                or centred != current_centred
                or bool(_LABEL_RE.match(line.text))
                or _underlined(previous, rules)
                # lines of one paragraph sit under each other
                or min(line.bbox[2], previous.bbox[2]) - max(line.bbox[0], previous.bbox[0]) <= 0
            )
            if not new and not centred:
                indented = line.bbox[0] - left > 0.8 * size  # first-line indent
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
                new = indented or right - previous.bbox[2] > 1.5 * size  # previous line ended early
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


def _needs_translation(text: str, source_language: str) -> bool:
    if source_language.lower().startswith("zh"):
        return bool(_CJK_RE.search(text))
    # A bare code ("J01-L3C", "R05-A") has nothing to translate; rewriting it
    # only risks changing it.
    from .pdf_pipeline import _IMMUTABLE_IDENTIFIER_RE

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
        "protected_tokens": rule_protected_tokens(text, dates),
        "style_signature": style,
        "context_before": "",
        "context_after": "",
    }
    return TranslationUnit(id=generate_unit_id(**data), **data)


_MAX_BATCH_ITEMS = 40

_PAGE_OF_EN = re.compile(r"Page\s+(\d+)\s+of\s+(\d+)", re.I)
_PAGE_OF_ZH = re.compile(r"第\s*(\d+)\s*页\s*[，,]?\s*共\s*(\d+)\s*页")


def _page_footer_translation(unit: TranslationUnit) -> str | None:
    """"Page 2 of 2" footers in one fixed form: translated separately, page 1
    came back as "第 1 页，共 2 页" and page 2 as "2 / 2"."""
    text = unit.source_text.strip()
    if unit.target_language.lower().startswith("zh") and (match := _PAGE_OF_EN.fullmatch(text)):
        return f"第{match.group(1)}页，共{match.group(2)}页"
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
) -> dict[str, object]:
    import fitz

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
        plans.append((paragraph, translations[unit.id], _region(page, paragraph, others, paragraph.bounds)))
    fitted = [_fit_size(doc[p.page_number - 1], p, text, region, p.bounds) for p, text, region in plans]

    # Paragraphs sharing a source style (body text, chapter headings, ...)
    # share one size, as the source does: the smallest any of them needed,
    # so every paragraph still fits its own place. Sizing each on its own
    # gave a page of body text in five different sizes.
    group_size: dict[tuple[object, ...], float] = {}
    for (paragraph, _, _), size in zip(plans, fitted):
        key = _style_key(paragraph)
        group_size[key] = min(group_size.get(key, size), size)

    shrunk: list[dict[str, object]] = []
    for page_number in sorted({p.page_number for p, _, _ in plans}):
        page = doc[page_number - 1]
        page_plans = [plan for plan in plans if plan[0].page_number == page_number]
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
        for paragraph, text, region in page_plans:
            size, written_box = _insert(page, paragraph, text, region, group_size[_style_key(paragraph)], paragraph.bounds)
            _refit_underline(page, paragraph, text, size, written_box, rules)
            if size < paragraph.size - 1e-6:
                shrunk.append({"page": page_number, "source_size": round(paragraph.size, 2), "size": size, "text": text[:60]})
    return {
        "paragraphs": len(translatable),
        "style_sizes": [{"source_size": key[0], "font": key[1], "size": size} for key, size in group_size.items()],
        "size_reduced": shrunk,
    }


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
    column = fitz.Rect(x0, y1, x1, page.rect.height)
    below = [r.y0 for r in obstacles if r.intersects(column) and r.y0 >= y1 - 0.5]
    bottom = min(below + [page.rect.height - 24.0]) - 1.0
    # Never reach up into a table (or text) directly above: the table pass
    # later clears its own area and would erase any glyph overlapping it.
    above = [r.y1 + 0.5 for r in obstacles if r.x0 < x1 and r.x1 > x0 and y0 - 1.5 <= r.y1 <= y0 + 0.5]
    top = max(above + [y0 - 1.0])
    return fitz.Rect(x0 - 0.5, top, x1 + 0.5, max(y1 + 1.0, bottom))


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
        fontfile = _font_file(first.font, text, page=page) or r"C:\Windows\Fonts\simhei.ttf"
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
    if indent > 0.8 * size:
        spaces = int(round(indent / max(font.text_length(pad, fontsize=size), 0.1)))
    content = pad * spaces + text if spaces else text
    if cjk and width:
        # insert_textbox() only breaks at spaces, so an unspaced Chinese
        # paragraph was cut wherever the width ran out -- inside "IG-541" or
        # an English word. Break it ourselves, between CJK characters only.
        content = _wrap_atomic_phrases(content, fontfile=fontfile, fontname=_font_alias(fontfile), fontsize=size, max_width=width - 1.0)
    return content, fontfile, _font_alias(fontfile), align


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
        color = _rgb(paragraph.lines[0].color)
        # A bold source heading stays bold: CJK faces here have no bold
        # file, so the glyphs are filled and outlined in the same colour.
        bold = _CJK_RE.search(content) and _is_bold(paragraph)
        written = page.insert_textbox(
            box, content, fontname=alias, fontfile=fontfile, fontsize=size,
            color=color, align=align, overlay=True,
            **({"render_mode": 2, "fill": color, "border_width": 0.04} if bold else {}),
        )
        if written >= 0 or size - _FONT_STEP < _ABSOLUTE_MIN_SIZE:
            return size, box
        size = round(size - _FONT_STEP, 2)


def _is_bold(paragraph: _Paragraph) -> bool:
    spans = paragraph.lines[0].spans
    bold = [bool(int(span.get("flags") or 0) & 16) or "bold" in str(span.get("font", "")).casefold() for span in spans]
    return sum(bold) * 2 > len(bold)


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

    def under_line(line: _VisualLine) -> list:
        x0, _, x1, y1 = line.bbox
        return [
            rule for rule in rules
            if y1 - 0.4 * line.size <= rule[0].y0 <= y1 + 0.6 * line.size
            and rule[0].x0 >= x0 - 2.0 and rule[0].x1 <= x1 + 2.0
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


def _fits(region: Any, text: str, fontfile: str, alias: str, size: float, align: int) -> bool:
    from .pdf_table import probe_textbox

    return probe_textbox(region.width, region.height, text, fontfile=fontfile, fontname=alias, fontsize=size, align=align)[0] >= 0


def _render_tables(
    source: Path,
    staged: Path,
    output: Path,
    tables: list[Any],
    cell_translations: dict[str, str],
    source_language: str,
    target_language: str,
    warnings: list[dict[str, object]],
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
            warnings.append({"table": f"page:{table.page_number}:table:{table.table_number}", "errors": [str(exc)]})
            continue
        good.append(table)
    pages: dict[int, dict[str, object]] = {}
    sized: list[Any] = []
    alignment: dict[str, tuple[int, bool]] = {}
    with fitz.open(source) as source_doc:
        for page_number in sorted({t.page_number for t in good}):
            page_tables = [t for t in good if t.page_number == page_number]
            alignment.update(_table_alignment(source_doc[page_number - 1], page_tables))
            size = _table_font_size(page_tables, cell_translations)
            sized.extend(
                dataclasses.replace(t, cells=tuple(dataclasses.replace(c, source_font_size=size) for c in t.cells))
                for t in page_tables
            )
            pages[page_number] = {"font_size": size, "tables": len(page_tables)}
    if not sized:
        shutil.copyfile(staged, output)
    else:
        # A cell with nothing to translate (a bare number, a code) keeps
        # its own source text.
        mapping = {
            c.id: ("" if c.is_empty else (cell_translations.get(c.id) or c.text))
            for t in sized for c in t.cells
        }
        sizes = [float(page["font_size"]) for page in pages.values()]
        # Every table page in one pass: each cell keeps its page's size
        # (fixed_cell_size); rewriting the whole PDF once per table page made
        # a 777-page file take hours.
        pdf_table.render_table_translations(
            staged,
            output,
            mapping,
            tables=sized,
            fontfile=_table_cell_font,
            minimum_font_size=min(sizes),
            initial_font_size=max(sizes),
            align=lambda cell: alignment.get(cell.id, (0, False))[0],
            middle_aligned=lambda cell: alignment.get(cell.id, (0, False))[1],
            spread_lines=False,
            fixed_cell_size=True,
            keep_unchanged=True,
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
        header_row = min((c.row for c in table.cells if c.rect is not None), default=1)
        for column in {c.column for c in table.cells}:
            body = [detected[c.id] for c in table.cells if c.column == column and c.row != header_row and c.id in detected]
            majority = (
                1 if sum(h for h, _ in body) * 2 > len(body) else 0,
                sum(1 for _, m in body if m) * 2 > len(body),
            ) if body else None
            for cell in table.cells:
                if cell.column == column and cell.id in detected:
                    alignment[cell.id] = detected[cell.id] if cell.row == header_row or majority is None else majority
    return alignment


def _table_font_size(tables: list[Any], cell_translations: dict[str, str]) -> float:
    """Largest size (from the source's own) at which every cell fits its original cell."""
    import fitz

    from . import pdf_table

    fonts: dict[str, Any] = {}

    def fits(cell: Any, size: float) -> bool:
        text = "" if cell.is_empty else (cell_translations.get(cell.id) or cell.text)
        if cell.rect is None or not text.strip():
            return True
        rect = fitz.Rect(cell.rect)
        normalised = pdf_table._normalise_render_text(text)
        fontfile = str(_table_cell_font(cell, normalised))
        font = pdf_table.cached_font(fontfile)
        pad = pdf_table._cell_fit_padding(rect, normalised, 2.0)
        width, height = rect.width - 2 * pad, rect.height - 2 * pad
        for paragraph in normalised.split("\n"):
            for atom, _ in pdf_table._tokenize_atoms_with_seps(paragraph):
                if any(font.text_length(word, fontsize=size) > width for word in atom.split(" ")):
                    return False
        wrapped = pdf_table._wrap_atomic_phrases(normalised, fontfile=fontfile, fontname="probe", fontsize=size, max_width=width)
        if pdf_table.probe_textbox(width, height, wrapped, fontfile=fontfile, fontname="probe", fontsize=size)[0] >= 0:
            return True
        return pdf_table.compact_line_height(width, height, wrapped, fontfile=fontfile, fontname="probe", fontsize=size) is not None

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

    while size - _FONT_STEP >= _ABSOLUTE_MIN_SIZE and not all_fit(size):
        size = round(size - _FONT_STEP, 2)
    return size
