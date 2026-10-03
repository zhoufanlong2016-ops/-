"""Vector-table extraction and cell-level PDF translation rendering.

The regular paragraph path is deliberately not used for tables by this module.
PyMuPDF's table finder supplies the geometry, while the caller supplies a
stable-ID translation mapping.  Text is removed with text-only redactions and
the translated text is written back inside the original cell rectangles.  A
cell which cannot fit at the configured readable-font floor fails closed.

The public functions are intentionally independent of the translation
provider so they can be used to patch a rendered candidate after the provider
has returned a validated batch.
"""

from __future__ import annotations

import functools
import threading

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import tempfile
import re
from typing import Any, Callable


@functools.lru_cache(maxsize=None)
def cached_font(fontfile: str) -> Any:
    """Load a font file once per process; fitz.Font objects are read-only here.

    Loading a CJK face (about 10 MB) took ~20 ms per call and was repeated for
    every cell and every candidate size.
    """
    import fitz

    return fitz.Font(fontfile=fontfile)


_PROBE_PAGE_SIZE = 14400.0
_PROBES_PER_PAGE = 40
_probe_pages: dict[tuple[str, str], dict[str, Any]] = {}
_probe_lock = threading.Lock()


def probe_textbox(
    width: float,
    height: float,
    text: str,
    *,
    fontfile: str,
    fontname: str,
    fontsize: float,
    align: int = 0,
    count_lines: bool = False,
    lineheight: float | None = None,
) -> tuple[float, int]:
    """insert_textbox()'s fit result (and line count) for a width x height box.

    A fresh probe document per test re-embedded the font and recomputed every
    glyph width (65k for a CJK face): ~0.1 s per probe.  Probes instead share
    one large scratch page per font, each in its own non-overlapping strip,
    so the font is embedded once.  The result does not depend on where the
    box sits or on other text on the page (verified identical).
    """
    import fitz

    if height + 30 > _PROBE_PAGE_SIZE or width + 20 > _PROBE_PAGE_SIZE:
        probe = fitz.open()
        try:
            page = probe.new_page(width=width + 20, height=height + 20)
            rect = fitz.Rect(0, 0, width, height)
            result = page.insert_textbox(rect, text, fontname=fontname, fontfile=fontfile, fontsize=fontsize, align=align, lineheight=lineheight)
            return result, _probe_line_count(page, None) if count_lines else 0
        finally:
            probe.close()
    with _probe_lock:
        key = (str(fontfile), fontname)
        state = _probe_pages.get(key)
        if state is None or state["y"] + height + 30 > _PROBE_PAGE_SIZE or state["count"] >= _PROBES_PER_PAGE:
            if state is not None:
                state["doc"].close()
            doc = fitz.open()
            state = _probe_pages[key] = {"doc": doc, "page": doc.new_page(width=_PROBE_PAGE_SIZE, height=_PROBE_PAGE_SIZE), "y": 0.0, "count": 0}
        rect = fitz.Rect(10, state["y"], 10 + width, state["y"] + height)
        state["y"] += height + 30
        state["count"] += 1
        result = state["page"].insert_textbox(rect, text, fontname=fontname, fontfile=fontfile, fontsize=fontsize, align=align, lineheight=lineheight)
        return result, _probe_line_count(state["page"], rect) if count_lines else 0


# insert_textbox() reserves a full line pitch plus the descent even for a
# single line, so a 7 pt value no longer fit the 9 pt row it came from and
# the whole table dropped to 6 pt. One line needs no inter-line room.


def compact_line_height(
    width: float, height: float, wrapped: str, *, fontfile: str, fontname: str, fontsize: float, align: int = 0
) -> float | None:
    """A line height (< 1) when single-line ``wrapped`` fits only with one."""
    if not wrapped.strip():
        return None
    if probe_textbox(width, height, wrapped, fontfile=fontfile, fontname=fontname, fontsize=fontsize, align=align)[0] >= 0:
        return None
    font = cached_font(fontfile)
    # insert_textbox() needs lineheight*size plus the descent: with the
    # ascent as line height one line takes exactly the glyph box height;
    # several lines keep the glyph pitch (SimHei's default adds 20 %).
    if "\n" in wrapped:
        compact = round(max(font.ascender - font.descender, 1.0) + 0.05, 3)
    else:
        compact = round(max(font.ascender, 0.8), 3)
    fits = probe_textbox(
        width, height, wrapped, fontfile=fontfile, fontname=fontname, fontsize=fontsize, align=align,
        lineheight=compact,
    )[0] >= 0
    return compact if fits else None


def _probe_line_count(page: Any, clip: Any) -> int:
    return sum(
        len(block.get("lines", []))
        for block in page.get_text("dict", clip=clip).get("blocks", [])
        if block.get("type") == 0
    )


class PdfTableError(ValueError):
    """Base class for table extraction, mapping, and rendering failures."""


class PdfTableExtractionError(PdfTableError):
    """Raised when a table cannot be represented safely as logical cells."""


class PdfTableMappingError(PdfTableError):
    """Raised when a translation response is incomplete or ambiguous."""


class PdfTableFitError(PdfTableError):
    """Raised when translated text does not fit at the minimum font size."""

    def __init__(self, message: str, *, cell_id: str | None = None) -> None:
        super().__init__(message)
        self.cell_id = cell_id


# "2.1.2 Liaison..." milestone sub-items are list items too.
_TABLE_LIST_ITEM_RE = re.compile(r"^\s*(?:\d+(?:\.\d+)+\.?\s*(?=\S)|\d+[.)]\s+|[A-Za-z][.)]\s+|[-*•]\s+)")

# A non-breaking space (U+00A0) between a number and its unit, or after a
# short list marker, was tried here to stop insert_textbox() from
# stranding a unit or marker alone at a line break. Reverted: on this
# project own embedded CJK font, after the page fonts are subset for
# publishing, that same character renders as a visible tofu box wherever
# it landed (observed: "6.<box>Volume 2..." and "(EL)<box>System") --
# corrupting the page is worse than the wrap it was meant to fix, and a
# word-joiner (U+2060) is not a working substitute either: this font has
# no glyph for it either, and PyMuPDF own wrapper still breaks the line
# at that position anyway. Left unsolved rather than traded for something
# worse; revisit only with a fix verified against a real, fully-published
# (redacted, subset, saved) page, not an isolated probe render.


# "(IG-541) 或" is a code, not a "541)" list marker: a hyphen, slash or dot
# before the digits joins them to what precedes; so does an opening bracket
# ("六 (06) 台" is a quantity, not item "06)").
_INLINE_LIST_MARKER_RE = re.compile(r"(?<![A-Za-z0-9./(（-])(\d{1,3}[.)]|[A-Za-z][.)]|-)\s+")


def _break_inline_list_markers(text: str) -> str:
    """Force a line break before each list marker found inline in the text.

    A provider translating a whole multi-item cell as one string returns it
    as ONE continuous run with no real line breaks at all -- confirmed
    directly against this project's own real translations: a bulleted
    cell's "-"/"•" separators, and a single reference letter like "C.",
    come back with plain spaces around them, never a "\\n". Left alone,
    PyMuPDF's own insert_textbox() wraps that flat string purely by how
    much fits per line, and a short marker frequently ends up the last
    thing that fits -- reading as though it trails the PREVIOUS item
    ("...for offices; -" at a line's end) purely by coincidence of line
    width, not because of anything meaningful in the string. Inserting a
    genuine "\\n" before each marker removes that coincidence entirely: the
    marker now always leads its own line, exactly like a hand-typed list.
    """
    return _INLINE_LIST_MARKER_RE.sub(lambda match: "\n" + match.group(1) + " ", text)


def _normalise_render_text(text: str, *, keep_line_breaks: bool = False) -> str:
    """Convert provider visual line breaks into reflowable table text.

    A plain paragraph returned with embedded newlines must be allowed to wrap
    at the cell's actual width.  Explicit numbered/bulleted lists retain their
    line boundaries because those are semantic separators rather than visual
    extraction artifacts.
    """

    # "*" bullet (U+2022) is swapped for a plain hyphen before anything else
    # touches this text. PyMuPDF's font subsetting on publish (subset_fonts()
    # + garbage=3 in _atomic_save) corrupts this specific glyph's outline on
    # this project's own embedded CJK font -- confirmed directly against the
    # real render_table_translations()+publish path, not just an isolated
    # insert_textbox() probe: the character survives the FIRST render (a
    # correct, visible bullet dot) but is redrawn as a blank ".notdef" box
    # once the file is actually subset and saved, with its own ToUnicode
    # entry left pointing at U+0000 -- the exact documented failure mode
    # repair_pdf_text_cmaps() already treats as a corrupted space, except
    # this glyph draws visible ink instead, so that repair (which only ever
    # remaps the character code, never repaints geometry) cannot restore it.
    # A hyphen is both already an accepted list marker to _TABLE_LIST_ITEM_RE
    # below and verified to survive the identical publish path intact.
    text = text.replace("•", "-")
    text = _break_inline_list_markers(text)
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    if keep_line_breaks:
        # The source was sent item by item (one item per line), so every
        # line break in the translation is a real one.
        return "\n".join(lines)
    if any(_TABLE_LIST_ITEM_RE.match(line) for line in lines):
        # Keep breaks only when a new numbered/bulleted item starts.  Provider
        # line extraction often inserts visual breaks inside the same item;
        # those must become spaces so the textbox can reflow the item at the
        # actual cell width.
        grouped: list[str] = []
        for line in lines:
            if _TABLE_LIST_ITEM_RE.match(line) or not grouped:
                grouped.append(line)
            else:
                grouped[-1] = _join_wrapped(grouped[-1], line)
        return "\n".join(grouped)
    joined = lines[0]
    for line in lines[1:]:
        joined = _join_wrapped(joined, line)
    return joined


_CJK_EDGE_RE = re.compile(r"[　-〿㐀-鿿豈-﫿＀-￯]")


def _join_wrapped(left: str, right: str) -> str:
    """Rejoin a wrapped line: a space between English words, none where
    either side is Chinese ("第一部分，" + "第2节", not "第一部分， 第2节")."""
    if _CJK_EDGE_RE.match(left[-1:]) or _CJK_EDGE_RE.match(right[:1]):
        return left + right
    return left + " " + right


@dataclass(frozen=True, slots=True)
class PdfTableCell:
    """One logical table cell, including empty and merged placeholders.

    ``row`` and ``column`` are one-based.  ``rect`` is ``None`` only for a
    logical grid slot that is covered by a merged cell in PyMuPDF's table
    representation.  Such a placeholder must still have a translation entry
    (normally an empty string) so response completeness is explicit.
    """

    id: str
    page_number: int
    table_number: int
    row: int
    column: int
    rect: tuple[float, float, float, float] | None
    text: str
    source_font_size: float | None = None

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()

    @property
    def is_merged_placeholder(self) -> bool:
        return self.rect is None

    def as_payload(self) -> dict[str, str]:
        """Return the stable provider payload for this cell."""

        return {"id": self.id, "text": self.text}


@dataclass(frozen=True, slots=True)
class PdfTable:
    """A vector table and its complete logical cell grid."""

    page_number: int
    table_number: int
    rect: tuple[float, float, float, float]
    row_count: int
    column_count: int
    cells: tuple[PdfTableCell, ...]

    @property
    def id(self) -> str:
        return f"pdf:p{self.page_number}:t{self.table_number}"

    @property
    def cell_map(self) -> dict[str, PdfTableCell]:
        return {cell.id: cell for cell in self.cells}

    @property
    def cell_ids(self) -> tuple[str, ...]:
        return tuple(cell.id for cell in self.cells)

    def payload(self, *, include_empty: bool = True) -> list[dict[str, str]]:
        """Return cells in stable row-major order for a batch request."""

        cells = self.cells if include_empty else tuple(cell for cell in self.cells if not cell.is_empty)
        return [cell.as_payload() for cell in cells]


@dataclass(frozen=True, slots=True)
class PdfTableTranslation:
    """A provider response item used to detect duplicate IDs in sequences."""

    id: str
    text: str


@dataclass(frozen=True, slots=True)
class PdfTableRenderReport:
    """Audit information returned after a successful table rewrite."""

    destination: Path
    table_count: int
    cell_count: int
    rendered_cell_count: int
    font_sizes: tuple[tuple[str, float], ...]
    drawing_counts: tuple[tuple[int, int, int], ...]
    restored_link_count: int = 0

    @property
    def font_size_map(self) -> dict[str, float]:
        return dict(self.font_sizes)


def _fitz() -> Any:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - exercised without the PDF extra
        raise RuntimeError("PDF table support requires PyMuPDF") from exc
    return fitz


def _rect_tuple(rect: Any, *, allow_none: bool = True) -> tuple[float, float, float, float] | None:
    if rect is None:
        if allow_none:
            return None
        raise PdfTableExtractionError("table returned a missing rectangle")
    if all(hasattr(rect, name) for name in ("x0", "y0", "x1", "y1")):
        values = (rect.x0, rect.y0, rect.x1, rect.y1)
    else:
        try:
            values = tuple(rect)
        except TypeError as exc:
            raise PdfTableExtractionError(f"invalid table rectangle: {rect!r}") from exc
    if len(values) != 4:
        raise PdfTableExtractionError(f"invalid table rectangle: {rect!r}")
    result = tuple(float(value) for value in values)
    if not all(math.isfinite(value) for value in result):
        raise PdfTableExtractionError(f"non-finite table rectangle: {rect!r}")
    x0, y0, x1, y1 = result
    if x1 <= x0 or y1 <= y0:
        raise PdfTableExtractionError(f"non-positive table rectangle: {rect!r}")
    return result


def _normalise_page_numbers(page_numbers: Iterable[int] | None, page_count: int) -> tuple[int, ...]:
    if page_numbers is None:
        return tuple(range(1, page_count + 1))
    try:
        values = tuple(int(value) for value in page_numbers)
    except (TypeError, ValueError) as exc:
        raise ValueError("page_numbers must contain one-based integers") from exc
    if len(set(values)) != len(values):
        raise ValueError("page_numbers contains duplicates")
    if any(value < 1 or value > page_count for value in values):
        raise ValueError(f"page_numbers must be between 1 and {page_count}")
    return values


def _dominant_font_size(spans_by_size: dict[float, int], rect: Any) -> float | None:
    """Return the font size covering the most characters inside ``rect``.

    A cell can mix sizes (a bold run-in label ahead of regular body text,
    say); the size backing the most text is the one that actually reads
    as "this cell's font size" to a reader, not whichever span happens to
    come first in the page's own content-stream order.
    """
    if not spans_by_size:
        return None
    return max(spans_by_size.items(), key=lambda item: item[1])[0]


def _collect_cell_font_sizes(page: Any, cell_rects: Sequence[Any]) -> list[dict[float, int]]:
    """For each cell rect, tally character counts by font size within it.

    One pass over the page's own text spans, tested against every still-
    unmatched cell rect, is far cheaper than re-querying get_text() per
    cell on a page with a large table -- this project's own tables run to
    dozens of rows.
    """
    import fitz

    tallies: list[dict[float, int]] = [dict() for _ in cell_rects]
    fitz_rects = [fitz.Rect(r) if r is not None else None for r in cell_rects]
    for block in page.get_text("dict").get("blocks", ()):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                span_text = span.get("text", "")
                if not span_text.strip():
                    continue
                span_box = fitz.Rect(span.get("bbox", ()))
                center = fitz.Point((span_box.x0 + span_box.x1) / 2, (span_box.y0 + span_box.y1) / 2)
                for index, rect in enumerate(fitz_rects):
                    if rect is not None and center in rect:
                        size = round(float(span.get("size") or 0), 2)
                        if size > 0:
                            tallies[index][size] = tallies[index].get(size, 0) + len(span_text.strip())
                        break
    return tallies


def _table_cells(table: Any, page_number: int, table_number: int) -> tuple[PdfTableCell, ...]:
    try:
        row_count = int(table.row_count)
        column_count = int(table.col_count)
    except (AttributeError, TypeError, ValueError) as exc:
        raise PdfTableExtractionError("PyMuPDF returned a table without valid dimensions") from exc
    if row_count <= 0 or column_count <= 0:
        raise PdfTableExtractionError("PyMuPDF returned an empty table")

    try:
        extracted = table.extract()
    except Exception as exc:  # pragma: no cover - implementation-specific PyMuPDF errors
        raise PdfTableExtractionError(f"failed to extract table p{page_number}:t{table_number}") from exc
    if not isinstance(extracted, Sequence) or len(extracted) != row_count:
        raise PdfTableExtractionError(
            f"table p{page_number}:t{table_number} returned {len(extracted) if extracted is not None else 0} rows; "
            f"expected {row_count}"
        )

    rows = getattr(table, "rows", None)
    if rows is not None and len(rows) != row_count:
        raise PdfTableExtractionError(
            f"table p{page_number}:t{table_number} returned {len(rows)} geometry rows; expected {row_count}"
        )

    # Current PyMuPDF exposes row.cells, which preserves None placeholders for
    # merged cells.  The fallback is retained for compatible table-like
    # objects used by callers/tests that expose only a flat cells sequence.
    flat_cells = list(getattr(table, "cells", ()) or ())
    flat_index = 0
    result: list[PdfTableCell] = []
    for row_index in range(row_count):
        source_row = extracted[row_index]
        if source_row is None:
            source_row = []
        if not isinstance(source_row, Sequence) or len(source_row) != column_count:
            raise PdfTableExtractionError(
                f"table p{page_number}:t{table_number} returned an invalid row {row_index + 1}"
            )

        if rows is not None:
            geometry_row = list(getattr(rows[row_index], "cells", ()) or ())
            if len(geometry_row) != column_count:
                raise PdfTableExtractionError(
                    f"table p{page_number}:t{table_number} returned an invalid geometry row {row_index + 1}"
                )
        else:
            geometry_row = []
            while len(geometry_row) < column_count and flat_index < len(flat_cells):
                geometry_row.append(flat_cells[flat_index])
                flat_index += 1
            if len(geometry_row) != column_count:
                raise PdfTableExtractionError(
                    f"table p{page_number}:t{table_number} has no complete cell geometry"
                )

        for column_index in range(column_count):
            raw_text = source_row[column_index]
            text = "" if raw_text is None else str(raw_text).replace("\r\n", "\n").replace("\r", "\n")
            rect = _rect_tuple(geometry_row[column_index])
            if rect is None and text.strip():
                raise PdfTableExtractionError(
                    f"table p{page_number}:t{table_number}:r{row_index + 1}:c{column_index + 1} "
                    "contains text but no cell geometry"
                )
            cell_id = f"pdf:p{page_number}:t{table_number}:r{row_index + 1}:c{column_index + 1}"
            result.append(
                PdfTableCell(
                    id=cell_id,
                    page_number=page_number,
                    table_number=table_number,
                    row=row_index + 1,
                    column=column_index + 1,
                    rect=rect,
                    text=text,
                )
            )

    # Read back each cell's own source font size so the renderer can start
    # from it (this project's stated policy elsewhere -- prefer the
    # source's own size, shrink only on real overflow -- otherwise never
    # reaches table cells at all, which is exactly the content most of
    # this project's own documents are made of). Best-effort: a table
    # object from a test double or an older PyMuPDF without a page
    # back-reference simply leaves every cell's source_font_size unset,
    # falling back to the renderer's own default exactly as before.
    page = getattr(table, "page", None)
    if page is not None:
        from dataclasses import replace

        cell_rects = [cell.rect for cell in result]
        tallies = _collect_cell_font_sizes(page, cell_rects)
        result = [
            replace(cell, source_font_size=_dominant_font_size(tally, cell.rect))
            if cell.rect is not None
            else cell
            for cell, tally in zip(result, tallies)
        ]
    return tuple(result)


def _merge_phantom_rows(
    cells: tuple[PdfTableCell, ...],
    row_count: int,
    column_count: int,
    dividers: tuple[float, ...] | None = None,
    table_rect: tuple[float, float, float, float] | None = None,
) -> tuple[PdfTableCell, ...]:
    """Fold a row created by a stray line back into the cell above it.

    PyMuPDF's vector-table finder (``strategy="lines_strict"``) treats any
    sufficiently long, thin line as a row divider -- including a
    hyperlink's own decorative underline, which runs across one text column
    only. That turns one multi-line reply into several one-column "rows"
    whose other columns are placeholders (``rect=None``).

    What makes such a row fake is the line above it: a real row starts at a
    divider ruled across the whole table, a phantom one at a line inside a
    single column. Judging by the cell count alone (one real cell) also
    folded genuine full-width rows -- a "Note:" spanning both columns was
    glued under the cell to its upper left, and half of it was never
    translated. With ``dividers`` (y positions of full-width rules) a row
    starting on a full-width rule is never folded.
    """
    from dataclasses import replace

    by_row: dict[int, list[PdfTableCell]] = {}
    for cell in cells:
        by_row.setdefault(cell.row, []).append(cell)

    def starts_on_divider(cell: PdfTableCell) -> bool:
        return dividers is not None and cell.rect is not None and any(abs(cell.rect[1] - y) <= 1.5 for y in dividers)

    active: dict[int, str] = {}
    merged: dict[str, PdfTableCell] = {cell.id: cell for cell in cells}
    for row in range(1, row_count + 1):
        real = [cell for cell in by_row.get(row, ()) if cell.rect is not None]
        if len(real) == 1 and column_count > 1 and real[0].column in active and not starts_on_divider(real[0]):
            phantom = real[0]
            target = merged[active[phantom.column]]
            assert target.rect is not None and phantom.rect is not None
            merged_text = target.text + ("\n" + phantom.text if phantom.text.strip() else "")
            merged_rect = (target.rect[0], target.rect[1], target.rect[2], phantom.rect[3])
            merged[target.id] = replace(target, text=merged_text, rect=merged_rect)
            merged[phantom.id] = replace(phantom, text="")
            continue
        for cell in real:
            active[cell.column] = cell.id

    return tuple(merged[cell.id] for cell in cells)


def _full_width_dividers(page: Any, rect: tuple[float, float, float, float]) -> tuple[float, ...]:
    """y positions of horizontal rules that, joined up, cross the whole table.

    A row divider is often drawn as one short segment per column; the
    segments at one height are added up before comparing with the width.
    """
    width = rect[2] - rect[0]
    segments: list[tuple[float, float, float]] = []
    for drawing in page.get_drawings():
        for item in drawing.get("items", ()):
            if item[0] == "l" and abs(item[1].y - item[2].y) <= 1.0:
                x0, x1 = sorted((item[1].x, item[2].x))
                segments.append(((item[1].y + item[2].y) / 2, x0, x1))
            elif item[0] == "re" and item[1].height <= 2.0:
                box = item[1]
                segments.append(((box.y0 + box.y1) / 2, box.x0, box.x1))
    found: list[float] = []
    for y, _, _ in segments:
        if any(abs(y - other) <= 1.0 for other in found):
            continue
        spans = sorted((x0, x1) for sy, x0, x1 in segments if abs(sy - y) <= 1.0)
        covered, reach = 0.0, rect[0]
        for x0, x1 in spans:
            x0, x1 = max(x0, reach), min(x1, rect[2])
            if x1 > x0:
                covered += x1 - x0
                reach = x1
        if covered >= 0.9 * width:
            found.append(y)
    return tuple(found)


def extract_tables_from_document(
    document: Any, *, page_numbers: Iterable[int] | None = None, merge_phantom_rows: bool = True
) -> tuple[PdfTable, ...]:
    """Extract vector tables from an open PyMuPDF document.

    Table numbering is one-based per page and is the order returned by
    ``page.find_tables()``.  Page numbers are one-based and preserve the
    original document numbering when a page filter is used.

    "lines_strict" (a fully drawn grid on every side of every cell) is
    tried first since it is the least likely of PyMuPDF's strategies to
    mis-segment ordinary prose into a false table. Some real data tables
    in the wild are drawn with only a partial rule -- an outer border and
    a header underline, no per-cell grid -- and "lines_strict" finds
    nothing on them at all (observed: a 68-row lift-station schedule,
    whitespace-aligned with no interior lines, silently skipped as "no
    vector tables" and left completely untranslated). Falling back to the
    looser "lines" strategy only when "lines_strict" finds nothing picks
    that case up without changing anything for a page "lines_strict"
    already handles. The looser-still "text" strategy is not used here:
    on this project's own drawings it over-segments a 6-column table into
    12, which is worse than not detecting a table at all.
    """

    selected_pages = _normalise_page_numbers(page_numbers, int(document.page_count))
    tables: list[PdfTable] = []
    for page_number in selected_pages:
        page = document[page_number - 1]
        try:
            finder = page.find_tables(strategy="lines_strict")
            page_tables = tuple(getattr(finder, "tables", ()) or ())
            if not page_tables:
                finder = page.find_tables(strategy="lines")
                page_tables = tuple(getattr(finder, "tables", ()) or ())
        except Exception as exc:  # pragma: no cover - implementation-specific PyMuPDF errors
            raise PdfTableExtractionError(f"failed to find vector tables on page {page_number}") from exc
        for table_number, table in enumerate(page_tables, 1):
            rect = _rect_tuple(getattr(table, "bbox", None), allow_none=False)
            cells = _table_cells(table, page_number, table_number)
            if merge_phantom_rows:
                cells = _merge_phantom_rows(
                    cells, int(table.row_count), int(table.col_count), _full_width_dividers(page, rect), rect
                )
            tables.append(
                PdfTable(
                    page_number=page_number,
                    table_number=table_number,
                    rect=rect,
                    row_count=int(table.row_count),
                    column_count=int(table.col_count),
                    cells=cells,
                )
            )
    return tuple(tables)


def extract_pdf_tables(
    source_path: str | Path, *, page_numbers: Iterable[int] | None = None, merge_phantom_rows: bool = True
) -> tuple[PdfTable, ...]:
    """Open ``source_path`` and return all requested vector tables."""

    fitz = _fitz()
    document = fitz.open(Path(source_path))
    try:
        return extract_tables_from_document(document, page_numbers=page_numbers, merge_phantom_rows=merge_phantom_rows)
    finally:
        document.close()


def normalize_cell_translations(
    translations: Mapping[str, str] | Iterable[PdfTableTranslation | Mapping[str, str] | Sequence[str]],
) -> dict[str, str]:
    """Normalize provider output while detecting duplicate IDs.

    A mapping is accepted for already-validated responses.  A sequence may
    contain ``PdfTableTranslation``, ``{"id": ..., "text": ...}``
    (``output`` and ``translation`` are also accepted for the structured
    response shape), or two-item ``(id, text)`` entries; this is the form that
    exposes duplicate IDs and therefore should be used directly on provider
    responses.
    """

    result: dict[str, str] = {}
    if isinstance(translations, Mapping):
        items = translations.items()
    else:
        try:
            raw_items = iter(translations)
        except TypeError as exc:
            raise PdfTableMappingError("translations must be a mapping or an iterable of response items") from exc

        def item_pairs() -> Iterable[tuple[Any, Any]]:
            for item in raw_items:
                if isinstance(item, PdfTableTranslation):
                    yield item.id, item.text
                elif isinstance(item, Mapping):
                    if "id" not in item:
                        raise PdfTableMappingError("each translation item must contain id")
                    text_key = next((key for key in ("text", "output", "translation") if key in item), None)
                    if text_key is None:
                        raise PdfTableMappingError("each translation item must contain text/output/translation")
                    yield item["id"], item[text_key]
                elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) == 2:
                    yield item[0], item[1]
                else:
                    raise PdfTableMappingError("each translation item must be an id/text pair")

        items = item_pairs()

    for raw_id, raw_text in items:
        if not isinstance(raw_id, str) or not raw_id:
            raise PdfTableMappingError("translation IDs must be non-empty strings")
        if raw_id in result:
            raise PdfTableMappingError(f"duplicate translation ID: {raw_id}")
        if not isinstance(raw_text, str):
            raise PdfTableMappingError(f"translation text for {raw_id} must be a string")
        result[raw_id] = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    return result


def validate_table_translations(
    table: PdfTable,
    translations: Mapping[str, str] | Iterable[PdfTableTranslation | Mapping[str, str] | Sequence[str]],
    *, source_language: str = "auto", target_language: str = "zh",
) -> dict[str, str]:
    """Require exactly one translation for every logical cell in ``table``."""

    result = normalize_cell_translations(translations)
    expected = set(table.cell_ids)
    actual = set(result)
    missing = [cell_id for cell_id in table.cell_ids if cell_id not in actual]
    extra = sorted(actual - expected)
    if missing:
        raise PdfTableMappingError("missing cell translations: " + ", ".join(missing))
    if extra:
        raise PdfTableMappingError("unknown cell translation IDs: " + ", ".join(extra))
    for cell in table.cells:
        translated = result[cell.id]
        _validate_cell_names(cell, translated, source_language, target_language)
        if cell.is_empty and translated.strip():
            raise PdfTableMappingError(f"empty source cell {cell.id} cannot receive non-empty translation")
        if not cell.is_empty and not translated.strip():
            raise PdfTableMappingError(f"non-empty source cell {cell.id} has an empty translation")
    return result


def validate_pdf_table_translations(
    tables: Iterable[PdfTable],
    translations: Mapping[str, str] | Iterable[PdfTableTranslation | Mapping[str, str] | Sequence[str]],
    *, source_language: str = "auto", target_language: str = "zh",
) -> dict[str, str]:
    """Validate one complete mapping covering all supplied tables."""

    table_list = tuple(tables)
    result = normalize_cell_translations(translations)
    expected_ids = tuple(cell.id for table in table_list for cell in table.cells)
    expected = set(expected_ids)
    actual = set(result)
    missing = [cell_id for cell_id in expected_ids if cell_id not in actual]
    extra = sorted(actual - expected)
    if missing:
        raise PdfTableMappingError("missing cell translations: " + ", ".join(missing))
    if extra:
        raise PdfTableMappingError("unknown cell translation IDs: " + ", ".join(extra))
    cell_map = {cell.id: cell for table in table_list for cell in table.cells}
    for cell_id in expected_ids:
        cell = cell_map[cell_id]
        translated = result[cell_id]
        _validate_cell_names(cell, translated, source_language, target_language)
        if cell.is_empty and translated.strip():
            raise PdfTableMappingError(f"empty source cell {cell_id} cannot receive non-empty translation")
        if not cell.is_empty and not translated.strip():
            raise PdfTableMappingError(f"non-empty source cell {cell_id} has an empty translation")
    return result


def _validate_cell_names(cell: PdfTableCell, translated: str, source_language: str, target_language: str) -> None:
    from document_translator.translation_rules import validate_name_retention
    errors = validate_name_retention(cell.text, translated, source_language, target_language)
    if errors:
        raise PdfTableMappingError(f"{cell.id}: " + "; ".join(errors))


def _inset_rect(fitz: Any, rect: tuple[float, float, float, float], padding: float) -> Any:
    x0, y0, x1, y1 = rect
    inset = fitz.Rect(x0 + padding, y0 + padding, x1 - padding, y1 - padding)
    if inset.width <= 0 or inset.height <= 0:
        raise PdfTableFitError("table cell has no usable area after padding")
    return inset


def _cell_fit_padding(rect: Any, text: str, padding: float) -> float:
    """Keep short/narrow table labels from losing their whole fit margin.

    A fixed 2pt inset is appropriate for prose cells, but it consumes most of
    a compact header cell (for example ``序号``).  Preserve a small readable
    margin while allowing the glyphs to use the actual cell geometry.
    """
    if padding <= 0:
        return 0.0
    width = max(0.0, float(rect.x1 - rect.x0))
    height = max(0.0, float(rect.y1 - rect.y0))
    # Rows a few lines tall (a dense schedule: 3 lines in a 27.7 pt row) have
    # no room for a 2 pt inset at the source's own size either.
    if len(text.strip()) <= 4 or width < 36.0 or height < 48.0:
        return min(padding, 0.5)
    return padding


def _span_rects_for_cell(page: Any, cell_rect: Any, *, bottom_tolerance: float = 0.0) -> list[Any]:
    """Return text spans assigned to a cell using span centers first."""

    spans: list[Any] = []
    bottom = cell_rect.y1 + max(0.0, float(bottom_tolerance))
    data = page.get_text("dict", sort=False)
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                text = str(span.get("text", ""))
                if not text.strip():
                    continue
                raw_box = span.get("bbox")
                if not raw_box:
                    continue
                box = page.rect.__class__(raw_box)
                center_x = (box.x0 + box.x1) / 2
                center_y = (box.y0 + box.y1) / 2
                if (
                    cell_rect.x0 - 0.5 <= center_x <= cell_rect.x1 + 0.5
                    and cell_rect.y0 - 0.5 <= center_y <= bottom + 0.5
                ):
                    spans.append(box)
    return spans


def _font_alias(fontfile: Path) -> str:
    digest = hashlib.sha1(str(fontfile).encode("utf-8")).hexdigest()[:10]
    return f"pdfTable{digest}"


# A run of 2-3 short Latin/digit "words" (a unit value like "3,500 mm", a
# standard code like "NFPA 2001", a proper noun like "Gulshan e Ravi", a
# project code like "0074-PAK-01") must never be split at one of its own
# internal spaces. PyMuPDF's own insert_textbox() wraps CJK-mixed text by
# packing characters to the available width with no notion that these
# particular ASCII spaces are inside one semantic unit -- confirmed
# directly, including splitting mid-word once a too-long run no longer
# fits as one "word". An embedded non-breaking character (tried here
# earlier) does stop the split, but on this project's own font pipeline it
# also re-emerges as a visible corrupted glyph once the page is subset for
# publishing -- confirmed against a REAL multi-cell render, not just an
# isolated probe -- so it was reverted rather than traded for that. This
# regex identifies the same phrases for a DIFFERENT purpose: choosing
# where _wrap_atomic_phrases() below is and is not allowed to place a line
# break, using nothing but ordinary characters.
#
# Capped at 3 words (not the wider range every one of the examples above
# actually needs): a plain narrow label column translated from Chinese
# ("生产部门负责人" -> "Head of Production Department", observed directly
# overflowing a table's narrow first column) is FOUR ordinary English
# words with no digit and no proper noun in sight, and used to match this
# same pattern at a 6-word cap -- silently forbidding a wrap point in the
# middle of an ordinary phrase that has no reason at all to stay on one
# line, in a column already too narrow to hold it as a single run.
_ATOMIC_PHRASE_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9.,&/-]{0,19}(?: [A-Za-z0-9][A-Za-z0-9.,&/-]{0,19}){1,2}"
)


_CJK_CHAR_CLASS = r"　-〿㐀-䶿一-鿿豈-﫿＀-￯"
_ATOM_TOKEN_RE = re.compile(
    r"(?P<phrase>" + _ATOMIC_PHRASE_RE.pattern + r")"
    r"|(?P<space>\s+)"
    r"|(?P<cjk>[" + _CJK_CHAR_CLASS + r"])"
    r"|(?P<word>[^\s" + _CJK_CHAR_CLASS + r"]+)"
)


def _tokenize_atoms_with_seps(text: str) -> list[tuple[str, str]]:
    """Split text into (atom, trailing_separator) pairs, preserving original spacing.

    A recognised phrase is one atom (its OWN internal spaces are part of
    the atom, never a break point); anything else is walked one character
    at a time, so a CJK character is always its own atom (that script uses
    no inter-word spaces) while a run of other characters simply comes
    through as consecutive single-character atoms that the packer below
    will naturally keep adjacent (no whitespace between them to record).
    ``trailing_separator`` is "" or a single " ", copied from whether real
    whitespace followed this atom in the ORIGINAL text -- never invented --
    so re-wrapping never adds or removes a space the translation didn't
    already have (e.g. "编号5附表" stays spaceless; "132 kV" keeps its one).
    """
    tokens: list[tuple[str, str]] = []
    for match in _ATOM_TOKEN_RE.finditer(text):
        if match.lastgroup == "space":
            if tokens:
                atom, _ = tokens[-1]
                tokens[-1] = (atom, " ")
            continue
        if match.lastgroup == "phrase" and not _is_atomic_phrase(match.group(0)):
            words = match.group(0).split(" ")
            tokens.extend((word, " ") for word in words[:-1])
            tokens.append((words[-1], ""))
            continue
        tokens.append((match.group(0), ""))
    return tokens


def _split_overwide_phrases(
    pairs: list[tuple[str, str]], width_of: Callable[[str], float], max_width: float
) -> list[tuple[str, str]]:
    """Break a multi-word atom back into words when it cannot fit one line.

    Keeping a phrase together is a preference, not a rule: placed whole on a
    line it cannot fit, insert_textbox() splits it mid-word instead (a
    translated job title like "Relevant Functional Departments" in a narrow
    label column). Breaking between its own words is always better.
    """
    result: list[tuple[str, str]] = []
    for atom, sep in pairs:
        if " " in atom and width_of(atom) > max_width:
            words = atom.split(" ")
            result.extend((word, " ") for word in words[:-1])
            result.append((words[-1], sep))
        else:
            result.append((atom, sep))
    return result


def _is_atomic_phrase(phrase: str) -> bool:
    """Only values, codes and proper names stay unbroken -- not plain prose.

    _ATOMIC_PHRASE_RE matches ANY run of 2-3 short words, so ordinary prose
    ("for business projects") was being packed in rigid three-word chunks
    that could only wrap between chunks, leaving visibly ragged, early line
    breaks across a whole translated table. Keep a phrase together only when
    it contains a digit ("3,500 mm", "NFPA 2001") or every substantive word
    is capitalised ("Gulshan e Ravi", "Head of Production"); short connector
    words of one or two letters are ignored for that test.
    """
    if any(ch.isdigit() for ch in phrase):
        return True
    words = [word for word in phrase.split(" ") if len(word) > 2]
    return bool(words) and all(word[0].isupper() for word in words)


def _wrap_atomic_phrases(
    text: str,
    *,
    fontfile: str,
    fontname: str,
    fontsize: float,
    max_width: float,
) -> str:
    """Re-wrap ``text`` at real line breaks, never splitting an atomic phrase.

    Runs a small greedy packer per existing paragraph (each "\\n"-delimited
    segment _normalise_render_text() already produced stays its own
    paragraph -- this never merges two list items together): each atom
    from _tokenize_atoms_with_seps() is added to the current line, with
    its ORIGINAL separator reproduced exactly, if it still fits
    ``max_width`` at this exact font/size, otherwise it starts a new line.
    A single atom wider than ``max_width`` on its own is still placed
    rather than dropped -- unavoidable overflow, no worse than today's
    behaviour, just never triggered by a short phrase this function
    already knows to keep together.

    Width is measured with a real fitz.Font loaded from ``fontfile`` --
    fitz.get_text_length() only measures PyMuPDF's built-in fonts, never a
    caller-supplied file, so it cannot see this project's own embedded CJK
    and Latin faces at all.
    """
    font = cached_font(fontfile)

    def width_of(candidate: str) -> float:
        return font.text_length(candidate, fontsize=fontsize)

    out_paragraphs: list[str] = []
    for paragraph in text.split("\n"):
        pairs = _split_overwide_phrases(_tokenize_atoms_with_seps(paragraph), width_of, max_width)
        if not pairs:
            out_paragraphs.append(paragraph)
            continue
        # Widths add up (text_length() applies no kerning), so each atom and
        # separator is measured once: re-measuring the whole line per atom
        # made wrapping quadratic in the paragraph length.
        lines: list[list[tuple[str, str, float, float]]] = []
        current: list[tuple[str, str, float, float]] = []
        current_width = 0.0
        for atom, sep in pairs:
            entry = (atom, sep, width_of(atom), width_of(sep) if sep else 0.0)
            added = (current[-1][3] if current else 0.0) + entry[2]
            if not current or current_width + added <= max_width:
                current.append(entry)
                current_width += added
                continue
            # Chinese line-breaking rules: no closing punctuation at the start
            # of a line ("，因此") and no opening bracket at its end ("三（").
            following = [entry]
            if atom[:1] in _NO_LINE_START and len(current) > 1:
                following.insert(0, current.pop())
            while len(current) > 1 and current[-1][0][-1:] in _NO_LINE_END:
                following.insert(0, current.pop())
            lines.append(current)
            current = following
            current_width = sum(item[2] for item in current) + sum(item[3] for item in current[:-1])
        if current:
            lines.append(current)
        out_paragraphs.append("\n".join(
            "".join(atom + (sep if index < len(line) - 1 else "") for index, (atom, sep, _, _) in enumerate(line))
            for line in lines
        ))
    return "\n".join(out_paragraphs)


_NO_LINE_START = frozenset("，。、；：！？）」』”’》〉】…,.;:!?)]%")
_NO_LINE_END = frozenset("（「『“‘《〈【([")


def _fit_textbox(
    page: Any,
    rect: Any,
    text: str,
    *,
    fontfile: str,
    fontname: str,
    initial_font_size: float,
    minimum_font_size: float,
    font_step: float,
    align: int,
) -> tuple[float, int]:
    """Find a fitting point size while preferring fewer wrapped lines.

    A first-fit search can keep a large font even when a slightly smaller,
    still-readable size would place the next word on the current line.  Probe
    every candidate on an isolated page, then choose the minimum line count;
    ties prefer the largest point size. Also returns that size's own line
    count, which the caller uses to decide whether the cell's remaining
    height (a Chinese translation commonly wraps to far fewer lines than the
    English source the cell was originally sized for) is worth spreading the
    same lines into with taller line spacing, rather than leaving them
    huddled at the top of an otherwise mostly-blank cell.
    """

    import fitz

    size = float(initial_font_size)
    step_count = int(math.floor((size - minimum_font_size) / font_step + 1e-9))
    fitting: list[tuple[int, float]] = []
    for step_index in range(step_count + 1):
        candidate = max(minimum_font_size, round(size - step_index * font_step, 4))
        # Pre-wrapped per candidate size: how many atoms fit on one line
        # changes with the font size, so the same phrase-preserving layout
        # has to be recomputed for each size this loop tries, not just once
        # for the winning one.
        wrapped = _wrap_atomic_phrases(
            text, fontfile=fontfile, fontname=fontname, fontsize=candidate, max_width=rect.width
        )
        result, line_count = probe_textbox(
            rect.width, rect.height, wrapped,
            fontfile=fontfile, fontname=fontname, fontsize=candidate, align=align, count_lines=True,
        )
        if result < -1e-6:
            if compact_line_height(
                rect.width, rect.height, wrapped, fontfile=fontfile, fontname=fontname, fontsize=candidate, align=align
            ) is None:
                continue
            line_count = 1
        fitting.append((max(1, line_count), candidate))
    if fitting:
        # Keep the largest fitting size.  Horizontal utilisation is handled by
        # the condensed font selected by the caller; shrinking solely to save
        # a wrapped line is explicitly not allowed by the font policy.
        line_count, chosen_size = max(fitting, key=lambda item: item[1])
        return chosen_size, line_count
    raise PdfTableFitError(
        f"cell text does not fit at minimum font size {minimum_font_size:g}pt: {text[:100]!r}"
    )


# insert_textbox()'s own default line spacing (no explicit ``lineheight``)
# is fixed to the font's own metrics -- it has no idea how tall the cell
# it is filling actually is. A cell whose height was sized for the
# English source's own (longer) line-wrapped paragraph commonly holds a
# Chinese translation that only needs half as many lines at the same
# point size, leaving the back half of the cell blank while every line
# sits packed at its ordinary spacing up against the top. A small,
# fixed bump to the line spacing -- not a computed stretch to exactly
# fill whatever room happens to be left -- eases that packed-at-the-top
# look without visually turning a short paragraph into a stretched-out
# one; the font size and top alignment are both left exactly as they
# are (this project's font-size policy -- prefer the source size, never
# grow past it -- untouched). Only ever applied as an increase over
# PyMuPDF's own default for this font, and only when the cell actually
# has slack to spare (an already-full cell renders exactly as before).
_DEFAULT_LINE_HEIGHT_FACTOR = 1.35
_MODEST_LINE_HEIGHT_FACTOR = 1.5


def _fill_line_height(
    rect: Any,
    text: str,
    *,
    fontfile: str,
    fontname: str,
    font_size: float,
    line_count: int,
    align: int,
) -> float | None:
    """Pick a modest line-height bump, verified to actually still fit.

    A formula based on ``rect``'s height and the font's nominal line
    pitch is only ever an estimate -- real line pitch varies slightly by
    font and by how insert_textbox itself lays a given piece of text out
    (confirmed directly: the same formula that left comfortable room for
    one cell overflowed a different one by a fraction of a point,
    turning a cosmetic spacing tweak into ``PdfTableFitError`` for the
    entire table). Render the candidate for real on a disposable page
    and only keep it if PyMuPDF itself reports the text still fits;
    otherwise the cell is left at its normal, already-verified spacing.
    """
    import fitz

    if line_count <= 0 or font_size <= 0:
        return None
    natural = _DEFAULT_LINE_HEIGHT_FACTOR * font_size * line_count
    if rect.height <= natural:
        return None
    probe = fitz.open()
    try:
        probe_page = probe.new_page(width=rect.width + 20, height=rect.height + 20)
        probe_rect = fitz.Rect(0, 0, rect.width, rect.height)
        result = probe_page.insert_textbox(
            probe_rect,
            text,
            fontname=fontname,
            fontfile=fontfile,
            fontsize=font_size,
            lineheight=_MODEST_LINE_HEIGHT_FACTOR,
            align=align,
            overlay=True,
        )
        if result >= -1e-6:
            return _MODEST_LINE_HEIGHT_FACTOR
        return None
    finally:
        probe.close()


# A bold source cell (a table header: "Sr #", "Employer's Response") keeps
# its weight: the CJK faces used here have no bold file, so the glyphs are
# filled and outlined in black.
_BOLD_TEXT = {"render_mode": 2, "fill": (0, 0, 0), "color": (0, 0, 0), "border_width": 0.04}


def _cell_is_bold(page: Any, rect: Any) -> bool:
    spans = [
        span for block in page.get_text("dict", clip=rect).get("blocks", ())
        for line in block.get("lines", ()) for span in line.get("spans", ())
        if str(span.get("text", "")).strip()
    ]
    bold = [bool(int(span.get("flags") or 0) & 16) or "bold" in str(span.get("font", "")).casefold() for span in spans]
    return bool(bold) and sum(bold) * 2 > len(bold)


def _cell_alignment(
    align: int | Mapping[str, int] | Callable[[PdfTableCell], int],
    cell: PdfTableCell,
) -> int:
    """Resolve a stable alignment value for one cell.

    Column-aware alignment is useful for ordinary business tables: role/name
    columns are commonly centred while long responsibility text is left
    aligned.  A callable or ID mapping keeps that decision explicit without
    changing the extraction or rendering contract for callers that pass one
    integer.
    """

    value: object
    if callable(align):
        value = align(cell)
    elif isinstance(align, Mapping):
        value = align.get(cell.id, 0)
    else:
        value = align
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1, 2, 3):
        raise ValueError(
            f"alignment for {cell.id} must be 0 (left), 1 (center), 2 (right), or 3 (justify)"
        )
    return value


def _links_for_cell(page_links: list[dict], cell_rect: Any) -> list[dict]:
    """Return URI links whose centre falls inside this cell's rectangle."""
    matches: list[dict] = []
    for link in page_links:
        if link.get("kind") != 2:  # 2 == fitz.LINK_URI; internal/goto links carry no external URI to preserve
            continue
        rect = link.get("from")
        if rect is None:
            continue
        center_x = (rect.x0 + rect.x1) / 2
        center_y = (rect.y0 + rect.y1) / 2
        if cell_rect.x0 - 0.5 <= center_x <= cell_rect.x1 + 0.5 and cell_rect.y0 - 0.5 <= center_y <= cell_rect.y1 + 0.5:
            matches.append(link)
    return matches


def _underline_rects_in_cell(page: Any, cell_rect: Any, *, edge_margin: float = 2.0) -> list[Any]:
    """Find thin decorative lines sitting INSIDE a cell, not on its border.

    A hyperlink is commonly styled with its own underline drawn as a
    separate thin filled rectangle rather than a native PDF text
    decoration, so it survives a ``graphics=0`` redaction untouched --
    and then sits at whatever position the ORIGINAL, differently-laid-
    out text put it, cutting across the middle of the REFLOWED
    translation and looking like the paragraph was split into pieces.

    This only runs for a cell _links_for_cell() already confirmed held a
    hyperlink, so matching every thin interior line in that one cell
    (there can be more than one, one per originally-underlined wrapped
    line) is safe -- requiring real clearance from all four of the
    cell's own edges is what keeps it from ever matching a genuine table
    border, which sits exactly at the cell boundary by construction. The
    candidate this runs against may have reflowed the page from its own
    source coordinates (an earlier block's translation changing a row's
    height, say), so cell_rect -- this cell's OWN geometry on the
    document actually being edited -- is the only rectangle that is
    guaranteed to align with what is on the page now.
    """
    found: list[Any] = []
    for drawing in page.get_drawings():
        rect = drawing.get("rect")
        if rect is None:
            continue
        height = float(rect.y1 - rect.y0)
        width = float(rect.x1 - rect.x0)
        if height > 2.0 or width < 2.0:
            continue
        if not (cell_rect.x0 - 1.0 <= rect.x0 and rect.x1 <= cell_rect.x1 + 1.0):
            continue
        if not (cell_rect.y0 + edge_margin <= rect.y0 and rect.y1 <= cell_rect.y1 - edge_margin):
            continue
        # Inset 1.5pt off each end before redacting: this decoration's own
        # endpoints commonly sit exactly ON the cell's left/right edge --
        # the same x-coordinate the column's vertical grid line runs
        # along -- and graphics=2 removes ANY graphic overlapping the
        # redaction rectangle, not just one fully contained in it, so an
        # untouched rect here could also sweep away that vertical border.
        # A pure underline is drawn with zero height (y0 == y1): PyMuPDF's
        # own overlap test for graphics=2 does not register any overlap
        # against a degenerate, zero-area rectangle, so redacting the
        # exact drawing rect removes nothing at all even though it was
        # correctly identified here -- pad the height by half a point so
        # the redaction rectangle has genuine area to intersect against.
        x0 = rect.x0 + 1.5 if width > 3.0 else rect.x0
        x1 = rect.x1 - 1.5 if width > 3.0 else rect.x1
        inset = rect.__class__(x0, rect.y0 - 0.5, x1, rect.y1 + 0.5)
        found.append(inset)
    return found


def _resolve_cell_font(
    fontfile: str | Path | Mapping[str, str | Path] | Callable[[PdfTableCell, str], str | Path],
    cell: PdfTableCell,
    translated: str,
) -> Path:
    """Resolve the font file to render one cell's translated text with.

    Mirrors _cell_alignment's shape: a plain path applies to every cell
    (this module's original, table-wide behaviour), while a mapping or a
    callable lets the caller choose a different font per cell. A single
    fixed font cannot express this project's own Latin/CJK-aware font
    policy (document_translator.services.pdf_layout._font_file) -- an
    untranslated English identifier or place name left inside an
    otherwise-Chinese table should not necessarily be drawn with a CJK
    font file just because the rest of the table needs one.
    """
    if callable(fontfile):
        value = fontfile(cell, translated)
    elif isinstance(fontfile, Mapping):
        value = fontfile.get(cell.id)
        if value is None:
            raise ValueError(f"no font mapped for cell {cell.id}")
    else:
        value = fontfile
    path = Path(value).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"embedded font file does not exist: {path}")
    return path


def _atomic_save(document: Any, destination: Path) -> None:
    fd, temporary_name = tempfile.mkstemp(prefix=".pdf-table-", suffix=".pdf", dir=str(destination.parent))
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        # PyMuPDF embeds the complete font file when text is inserted. Keep
        # only the glyphs referenced by this document before publishing the
        # table-rendered PDF; otherwise a one-page table can carry several MB
        # of unused CJK font data.
        document.subset_fonts()
        document.save(temporary, garbage=3, deflate=True)
        # A hard link is used as the final publish operation so an existing
        # destination can never be silently replaced, even under a race.
        try:
            os.link(temporary, destination)
        except FileExistsError:
            raise
        except OSError as exc:
            raise PdfTableError(f"cannot publish PDF without overwrite: {destination}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def render_table_translations(
    source_path: str | Path,
    destination_path: str | Path,
    translations: Mapping[str, str] | Iterable[PdfTableTranslation | Mapping[str, str] | Sequence[str]],
    *,
    tables: Iterable[PdfTable] | None = None,
    fontfile: str | Path | Mapping[str, str | Path] | Callable[[PdfTableCell, str], str | Path],
    minimum_font_size: float = 6.0,
    initial_font_size: float = 10.0,
    font_step: float = 0.5,
    padding: float = 2.0,
    align: int | Mapping[str, int] | Callable[[PdfTableCell], int] = 0,
    page_numbers: Iterable[int] | None = None,
    page_links: Mapping[int, list[dict]] | None = None,
    middle_aligned: Callable[[PdfTableCell], bool] | None = None,
    spread_lines: bool = True,
    fixed_cell_size: bool = False,
    keep_unchanged: bool = False,
    keep_line_breaks: bool = False,
) -> PdfTableRenderReport:
    """Render complete table translations into a new PDF.

    ``middle_aligned`` marks cells whose text is centred vertically in its
    cell (a label column, a header row) rather than starting at the top.
    ``spread_lines=False`` keeps every cell at the same natural line spacing
    instead of loosening it to fill spare cell height.

    Only source text spans are redacted.  ``apply_redactions`` is explicitly
    called with ``images=0, graphics=0`` so table lines and other vector
    graphics remain untouched.  All cells are validated and all text boxes
    are fit-checked before the output is published.

    ``fontfile`` accepts a single path (applied to every cell, this
    module's original behaviour), a mapping keyed by cell ID, or a
    ``(cell, translated_text) -> path`` callable -- the same per-cell
    resolution shape ``align`` already uses -- so a caller can apply this
    project's Latin/CJK-aware font policy instead of one fixed font for
    the whole table.

    ``page_links`` overrides this page's ``get_links()`` result (one-based
    page number to that page's link list) for callers rendering onto a
    document that is not the original the links were authored in -- for
    example a candidate a different pipeline stage already re-rendered
    from the source, whose own render step dropped every link annotation
    it never touched itself, in an ORIGINAL layout whose page geometry
    still matches the source the links were captured from.
    """

    fitz = _fitz()
    source = Path(source_path)
    destination = Path(destination_path)
    if source.resolve() == destination.resolve():
        raise ValueError("source and destination paths must differ")
    if destination.exists():
        raise FileExistsError(f"destination already exists: {destination}")
    if not destination.parent.exists():
        raise FileNotFoundError(f"destination directory does not exist: {destination.parent}")
    if not (
        isinstance(fontfile, (str, Path)) or isinstance(fontfile, Mapping) or callable(fontfile)
    ):
        raise ValueError("fontfile must be a path, a cell-ID mapping, or a callable")
    if minimum_font_size <= 0:
        raise ValueError("minimum_font_size must be positive")
    if initial_font_size < minimum_font_size:
        raise ValueError("initial_font_size must be at least minimum_font_size")
    if font_step <= 0:
        raise ValueError("font_step must be positive")
    if padding < 0:
        raise ValueError("padding must be non-negative")
    if isinstance(align, int) and not isinstance(align, bool) and align not in (0, 1, 2, 3):
        raise ValueError("align must be 0 (left), 1 (center), 2 (right), or 3 (justify)")
    if isinstance(align, bool) or not isinstance(align, (int, Mapping)) and not callable(align):
        raise ValueError("align must be an integer, a cell-ID mapping, or a callable")

    document = fitz.open(source)
    source_page_count = int(document.page_count)
    source_page_sizes = tuple((float(page.rect.width), float(page.rect.height)) for page in document)
    source_page_rotations = tuple(int(page.rotation) for page in document)
    source_image_counts = tuple(len(page.get_images(full=True)) for page in document)
    temporary_fit_doc = fitz.open()
    try:
        table_list = tuple(tables) if tables is not None else extract_tables_from_document(
            document, page_numbers=page_numbers
        )
        if not table_list:
            raise PdfTableExtractionError("no vector tables found in selected pages")
        mapping = validate_pdf_table_translations(table_list, translations)

        # keep_unchanged: a cell whose text needs no translation (a row
        # number "1.") keeps its original glyphs, alignment and weight.
        unchanged_ids = {
            cell.id for table in table_list for cell in table.cells
            if keep_unchanged and not cell.is_empty and mapping[cell.id] == cell.text
        }
        cells_by_page: dict[int, list[PdfTableCell]] = {}
        last_row_cell_ids: set[str] = set()
        for table in table_list:
            cells_by_page.setdefault(table.page_number, []).extend(table.cells)
            last_row_cell_ids.update(
                cell.id for cell in table.cells if cell.row == table.row_count
            )

        # Plan redactions and fit sizes without mutating the source document.
        # font_by_cell/alias_by_path resolve per cell rather than once for
        # the whole table, so a caller's font policy (Latin vs CJK, source
        # font family) can differ cell by cell; _font_alias() already keys
        # the alias by the font FILE's own hash, so two different resolved
        # fonts never collide under one PDF font resource name.
        redactions_by_page: dict[int, list[Any]] = {}
        fitted_sizes: dict[str, float] = {}
        fitted_line_heights: dict[str, float | None] = {}
        compact_cells: set[str] = set()
        bold_cells: set[str] = set()
        fitted_texts: dict[str, str] = {}
        source_drawing_counts: dict[int, int] = {}
        font_by_cell: dict[str, Path] = {}
        alias_by_path: dict[Path, str] = {}
        # A cell that held a hyperlink loses it outright once the cell's
        # text is redacted: apply_redactions drops any link annotation
        # overlapping the redacted area, and nothing about a plain text
        # rewrite restores it. Capture every page's links up front (before
        # anything is touched) so each one can be re-attached, pointing at
        # the same URI, once its cell's translation is in place.
        decoration_redactions_by_page: dict[int, list[Any]] = {}
        links_by_cell: dict[str, list[dict]] = {}
        page_links_by_page: dict[int, list[dict]] = {}
        for page_number, cells in cells_by_page.items():
            page = document[page_number - 1]
            source_drawing_counts[page_number] = len(page.get_drawings())
            page_links_by_page[page_number] = (
                list(page_links[page_number]) if page_links is not None and page_number in page_links
                else list(page.get_links())
            )
            fit_page = temporary_fit_doc.new_page(width=page.rect.width, height=page.rect.height)
            for cell in cells:
                translated = _normalise_render_text(mapping[cell.id], keep_line_breaks=keep_line_breaks)
                if not translated.strip() or cell.id in unchanged_ids:
                    continue
                if cell.rect is None:
                    raise PdfTableExtractionError(f"translated cell has no geometry: {cell.id}")
                if cell.is_empty:
                    # validate_pdf_table_translations normally catches this;
                    # keep the guard next to rendering for integrators that
                    # construct PdfTable objects themselves.
                    raise PdfTableMappingError(f"empty source cell cannot be rendered: {cell.id}")
                cell_font = _resolve_cell_font(fontfile, cell, translated)
                font_by_cell[cell.id] = cell_font
                if cell_font not in alias_by_path:
                    alias_by_path[cell_font] = _font_alias(cell_font)
                cell_alias = alias_by_path[cell_font]
                cell_rect = fitz.Rect(cell.rect)
                # A renderer may repartition a cell's text spans while preserving
                # the page geometry.  Redact the complete cell rectangle so no
                # stale fragment (for example a clipped table header) survives
                # the overlay.  ``graphics=0`` keeps the original grid lines.
                redactions_by_page.setdefault(page_number, []).append(cell_rect)
                if _cell_is_bold(page, cell_rect):
                    bold_cells.add(cell.id)
                matched_links = _links_for_cell(page_links_by_page[page_number], cell_rect)
                if matched_links:
                    links_by_cell[cell.id] = matched_links
                    decoration_redactions_by_page.setdefault(page_number, []).extend(
                        _underline_rects_in_cell(page, cell_rect)
                    )
                fit_rect = _inset_rect(
                    fitz,
                    cell.rect,
                    _cell_fit_padding(cell_rect, translated, padding),
                )
                # Start from THIS cell's own source font size when it is
                # known, matching this project's stated policy for every
                # other kind of text on the page (prefer the source's own
                # size, shrink only on real overflow) -- table cells used
                # to always start from the caller's single, table-wide
                # ``initial_font_size`` (a flat 10pt default) regardless of
                # what the source actually used, systematically rendering
                # a translation smaller than its own source whenever the
                # source ran larger than that default.
                cell_initial_size = max(cell.source_font_size or initial_font_size, minimum_font_size)
                # fixed_cell_size: the caller already chose each cell's size
                # (one per table page), so several pages can be rendered in a
                # single open/save instead of rewriting the whole PDF per page.
                cell_minimum_size = cell_initial_size if fixed_cell_size else minimum_font_size
                try:
                    fitted_size, fitted_line_count = _fit_textbox(
                        fit_page,
                        fit_rect,
                        translated,
                        fontfile=str(cell_font),
                        fontname=cell_alias,
                        initial_font_size=cell_initial_size,
                        minimum_font_size=cell_minimum_size,
                        font_step=font_step,
                        align=_cell_alignment(align, cell),
                    )
                except PdfTableFitError as exc:
                    # Attach which cell actually failed: a caller translating
                    # into a language that runs wider than the source (an
                    # English translation of a Chinese table, observed
                    # directly overflowing a column sized for short Chinese
                    # phrases) can recover from this by growing that ONE
                    # row's height rather than giving up on the whole table,
                    # but only if it can identify which row that is without
                    # parsing this exception's own message text back apart.
                    raise PdfTableFitError(str(exc), cell_id=cell.id) from exc
                fitted_sizes[cell.id] = fitted_size
                wrapped_text = _wrap_atomic_phrases(
                    translated,
                    fontfile=str(cell_font),
                    fontname=cell_alias,
                    fontsize=fitted_size,
                    max_width=fit_rect.width,
                )
                fitted_texts[cell.id] = wrapped_text
                compact = compact_line_height(
                    fit_rect.width, fit_rect.height, wrapped_text, fontfile=str(cell_font), fontname=cell_alias,
                    fontsize=fitted_size, align=_cell_alignment(align, cell),
                )
                if compact:
                    compact_cells.add(cell.id)
                fitted_line_heights[cell.id] = compact if compact or not spread_lines else _fill_line_height(
                    fit_rect,
                    wrapped_text,
                    fontfile=str(cell_font),
                    fontname=cell_alias,
                    font_size=fitted_size,
                    line_count=fitted_line_count,
                    align=_cell_alignment(align, cell),
                )
            fit_page = None

        removed_decoration_counts: dict[int, int] = {}
        restored_link_count = 0
        for page_number, cells in cells_by_page.items():
            page = document[page_number - 1]
            # Remove only the specific decorations identified above -- a
            # separate, narrower redaction pass with graphics=2 so this
            # never touches the table's own border lines, which are left
            # to the graphics=0 pass immediately below exactly as before.
            decoration_rects = decoration_redactions_by_page.get(page_number, [])
            if decoration_rects:
                for box in decoration_rects:
                    page.add_redact_annot(box, fill=None)
                page.apply_redactions(images=0, graphics=2, text=0)
            removed_decoration_counts[page_number] = len(decoration_rects)

            rectangles = redactions_by_page.get(page_number, [])
            if rectangles:
                for box in rectangles:
                    page.add_redact_annot(box, fill=None)
                page.apply_redactions(images=0, graphics=0, text=0)
            expected_drawings = source_drawing_counts[page_number] - removed_decoration_counts[page_number]
            if len(page.get_drawings()) != expected_drawings:
                raise PdfTableError(f"vector graphics changed while redacting page {page_number}")

            for cell in cells:
                translated = _normalise_render_text(mapping[cell.id], keep_line_breaks=keep_line_breaks)
                if not translated.strip() or cell.id in unchanged_ids:
                    continue
                assert cell.rect is not None
                cell_font = font_by_cell[cell.id]
                cell_alias = alias_by_path[cell_font]
                cell_rect = fitz.Rect(cell.rect)
                fit_rect = _inset_rect(
                    fitz,
                    cell.rect,
                    _cell_fit_padding(cell_rect, translated, padding),
                )
                render_text = fitted_texts.get(cell.id, translated)
                if middle_aligned is not None and middle_aligned(cell) and cell.id not in compact_cells:
                    fitted_line_heights[cell.id] = None
                    probe = fitz.open()
                    try:
                        leftover = probe.new_page(width=page.rect.width, height=page.rect.height).insert_textbox(
                            fit_rect,
                            render_text,
                            fontname=cell_alias,
                            fontfile=str(cell_font),
                            fontsize=fitted_sizes[cell.id],
                            align=_cell_alignment(align, cell),
                        )
                    finally:
                        probe.close()
                    # A hair short of half, so glyph-metric rounding on the
                    # real page can never push the shifted text out of fit.
                    if leftover > 1.0:
                        fit_rect = fitz.Rect(fit_rect.x0, fit_rect.y0 + leftover / 2 - 0.5, fit_rect.x1, fit_rect.y1)
                result = page.insert_textbox(
                    fit_rect,
                    render_text,
                    fontname=cell_alias,
                    fontfile=str(cell_font),
                    fontsize=fitted_sizes[cell.id],
                    lineheight=fitted_line_heights.get(cell.id),
                    align=_cell_alignment(align, cell),
                    overlay=True,
                    **(_BOLD_TEXT if cell.id in bold_cells else {}),
                )
                if result < -1e-6 and fitted_line_heights.get(cell.id) is not None:
                    # The cosmetic line-spacing bump was verified to fit on a
                    # disposable probe page, but the real page's own already-
                    # embedded font resources (accumulated from earlier cells)
                    # can round glyph metrics a hair differently -- fall back
                    # to this cell's normal, unmodified spacing rather than
                    # failing the whole table over a purely cosmetic extra.
                    result = page.insert_textbox(
                        fit_rect,
                        render_text,
                        fontname=cell_alias,
                        fontfile=str(cell_font),
                        fontsize=fitted_sizes[cell.id],
                        align=_cell_alignment(align, cell),
                        overlay=True,
                        **(_BOLD_TEXT if cell.id in bold_cells else {}),
                    )
                if result < -1e-6:
                    raise PdfTableFitError(
                        f"cell {cell.id} no longer fits at {fitted_sizes[cell.id]:g}pt during rendering"
                    )
                # The whole reflowed cell becomes the new clickable area:
                # a translation rarely keeps the exact same wrapped-line
                # boundaries the original link rectangle was drawn for.
                for link in links_by_cell.get(cell.id, []):
                    page.insert_link({"kind": link.get("kind", 2), "from": cell_rect, "uri": link.get("uri", "")})
                    restored_link_count += 1

        _atomic_save(document, destination)
    finally:
        temporary_fit_doc.close()
        document.close()

    # Reopen the published file for structural and font embedding checks.
    output = fitz.open(destination)
    try:
        if output.page_count != source_page_count:
            raise PdfTableError("rendered PDF page count changed")
        # Every alias actually used on a page must be embedded there --
        # a table can legitimately draw more than one font per page now
        # (a Latin-only cell next to a Chinese one), so this checks the
        # full set rendered on that page, not one fixed alias.
        aliases_by_page: dict[int, set[str]] = {}
        for cell_id, cell_font in font_by_cell.items():
            page_number = int(cell_id.split(":")[1][1:])
            aliases_by_page.setdefault(page_number, set()).add(alias_by_path[cell_font])
        drawing_counts: list[tuple[int, int, int]] = []
        for page_number in range(1, output.page_count + 1):
            page = output[page_number - 1]
            drawings = len(page.get_drawings())
            images = len(page.get_images(full=True))
            source_images = source_image_counts[page_number - 1]
            expected_published_drawings = source_drawing_counts.get(page_number, drawings) - removed_decoration_counts.get(page_number, 0)
            if page_number in source_drawing_counts and drawings != expected_published_drawings:
                raise PdfTableError(f"published vector graphics changed on page {page_number}")
            if images < source_images:
                raise PdfTableError(f"published images decreased on page {page_number}")
            width, height = source_page_sizes[page_number - 1]
            if abs(float(page.rect.width) - width) > 1e-3 or abs(float(page.rect.height) - height) > 1e-3:
                raise PdfTableError(f"published page size changed on page {page_number}")
            if int(page.rotation) != source_page_rotations[page_number - 1]:
                raise PdfTableError(f"published page rotation changed on page {page_number}")
            drawing_counts.append((page_number, drawings, images))
            required_aliases = aliases_by_page.get(page_number)
            if required_aliases:
                embedded = {font[4] for font in page.get_fonts(full=True) if len(font) > 4}
                missing = required_aliases - embedded
                if missing:
                    raise PdfTableError(f"embedded font was not found on page {page_number}")
    finally:
        output.close()

    return PdfTableRenderReport(
        destination=destination,
        table_count=len(table_list),
        cell_count=sum(len(table.cells) for table in table_list),
        rendered_cell_count=len(fitted_sizes),
        font_sizes=tuple(fitted_sizes.items()),
        drawing_counts=tuple(drawing_counts),
        restored_link_count=restored_link_count,
    )


# Short aliases make the integration intent explicit for callers that refer to
# a PDF rewrite instead of a rendering operation.
render_pdf_tables = render_table_translations
validate_translation_mapping = validate_table_translations


__all__ = [
    "PdfTable",
    "PdfTableCell",
    "PdfTableError",
    "PdfTableExtractionError",
    "PdfTableFitError",
    "PdfTableMappingError",
    "PdfTableRenderReport",
    "PdfTableTranslation",
    "extract_pdf_tables",
    "extract_tables_from_document",
    "normalize_cell_translations",
    "render_pdf_tables",
    "render_table_translations",
    "validate_pdf_table_translations",
    "validate_table_translations",
    "validate_translation_mapping",
]
