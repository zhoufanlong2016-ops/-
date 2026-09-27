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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import tempfile
import re
from typing import Any, Callable


class PdfTableError(ValueError):
    """Base class for table extraction, mapping, and rendering failures."""


class PdfTableExtractionError(PdfTableError):
    """Raised when a table cannot be represented safely as logical cells."""


class PdfTableMappingError(PdfTableError):
    """Raised when a translation response is incomplete or ambiguous."""


class PdfTableFitError(PdfTableError):
    """Raised when translated text does not fit at the minimum font size."""


_TABLE_LIST_ITEM_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+")


def _normalise_render_text(text: str) -> str:
    """Convert provider visual line breaks into reflowable table text.

    A plain paragraph returned with embedded newlines must be allowed to wrap
    at the cell's actual width.  Explicit numbered/bulleted lists retain their
    line boundaries because those are semantic separators rather than visual
    extraction artifacts.
    """

    lines = [re.sub(r"\s+", " ", line).strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return ""
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
                grouped[-1] += " " + line
        return "\n".join(grouped)
    return " ".join(lines)


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
    return tuple(result)


def _merge_phantom_rows(
    cells: tuple[PdfTableCell, ...], row_count: int, column_count: int
) -> tuple[PdfTableCell, ...]:
    """Fold a row whose real content sits in exactly one column back into
    the nearest real cell above it in that same column.

    PyMuPDF's vector-table finder (``strategy="lines_strict"``) treats any
    sufficiently long, thin line as a row divider -- including a
    hyperlink's own decorative underline, which happens to run almost
    the full width of one text column. That turns one genuine multi-line
    reply into several one-column "rows": every OTHER column in such a
    row comes back as a merged-cell placeholder (``rect=None``), because
    nothing in the source actually divides them there. A genuine table
    row -- or a genuine rowspan's own starting row -- always has its own
    content, or a deliberate placeholder, in more than a single column;
    this signature (exactly one real column, everything else a
    placeholder) is specific enough to fold safely without ever touching
    an intentional table structure.
    """
    from dataclasses import replace

    by_row: dict[int, list[PdfTableCell]] = {}
    for cell in cells:
        by_row.setdefault(cell.row, []).append(cell)

    active: dict[int, str] = {}
    merged: dict[str, PdfTableCell] = {cell.id: cell for cell in cells}
    for row in range(1, row_count + 1):
        real = [cell for cell in by_row.get(row, ()) if cell.rect is not None]
        if len(real) == 1 and column_count > 1 and real[0].column in active:
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


def extract_tables_from_document(document: Any, *, page_numbers: Iterable[int] | None = None) -> tuple[PdfTable, ...]:
    """Extract vector tables from an open PyMuPDF document.

    Table numbering is one-based per page and is the order returned by
    ``page.find_tables(strategy="lines_strict")``.  Page numbers are one-based
    and preserve the original document numbering when a page filter is used.
    """

    selected_pages = _normalise_page_numbers(page_numbers, int(document.page_count))
    tables: list[PdfTable] = []
    for page_number in selected_pages:
        page = document[page_number - 1]
        try:
            finder = page.find_tables(strategy="lines_strict")
        except Exception as exc:  # pragma: no cover - implementation-specific PyMuPDF errors
            raise PdfTableExtractionError(f"failed to find vector tables on page {page_number}") from exc
        page_tables = tuple(getattr(finder, "tables", ()) or ())
        for table_number, table in enumerate(page_tables, 1):
            rect = _rect_tuple(getattr(table, "bbox", None), allow_none=False)
            cells = _table_cells(table, page_number, table_number)
            cells = _merge_phantom_rows(cells, int(table.row_count), int(table.col_count))
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


def extract_pdf_tables(source_path: str | Path, *, page_numbers: Iterable[int] | None = None) -> tuple[PdfTable, ...]:
    """Open ``source_path`` and return all requested vector tables."""

    fitz = _fitz()
    document = fitz.open(Path(source_path))
    try:
        return extract_tables_from_document(document, page_numbers=page_numbers)
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
    if len(text.strip()) <= 4 or width < 36.0 or height < 24.0:
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
) -> float:
    """Find a fitting point size while preferring fewer wrapped lines.

    A first-fit search can keep a large font even when a slightly smaller,
    still-readable size would place the next word on the current line.  Probe
    every candidate on an isolated page, then choose the minimum line count;
    ties prefer the largest point size.
    """

    import fitz

    size = float(initial_font_size)
    step_count = int(math.floor((size - minimum_font_size) / font_step + 1e-9))
    fitting: list[tuple[int, float]] = []
    for step_index in range(step_count + 1):
        candidate = max(minimum_font_size, round(size - step_index * font_step, 4))
        probe = fitz.open()
        try:
            probe_page = probe.new_page(width=page.rect.width, height=page.rect.height)
            result = probe_page.insert_textbox(
                rect,
                text,
                fontname=fontname,
                fontfile=fontfile,
                fontsize=candidate,
                align=align,
                overlay=True,
            )
            if result < -1e-6:
                continue
            line_count = sum(
                len(block.get("lines", []))
                for block in probe_page.get_text("dict").get("blocks", [])
                if block.get("type") == 0
            )
            fitting.append((max(1, line_count), candidate))
        finally:
            probe.close()
    if fitting:
        # Keep the largest fitting size.  Horizontal utilisation is handled by
        # the condensed font selected by the caller; shrinking solely to save
        # a wrapped line is explicitly not allowed by the font policy.
        return max(fitting, key=lambda item: item[1])[1]
    raise PdfTableFitError(
        f"cell text does not fit at minimum font size {minimum_font_size:g}pt: {text[:100]!r}"
    )


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
        document.save(temporary)
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
) -> PdfTableRenderReport:
    """Render complete table translations into a new PDF.

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
                translated = _normalise_render_text(mapping[cell.id])
                if not translated.strip():
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
                fitted_sizes[cell.id] = _fit_textbox(
                    fit_page,
                    fit_rect,
                    translated,
                    fontfile=str(cell_font),
                    fontname=cell_alias,
                    initial_font_size=initial_font_size,
                    minimum_font_size=minimum_font_size,
                    font_step=font_step,
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
                translated = _normalise_render_text(mapping[cell.id])
                if not translated.strip():
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
                result = page.insert_textbox(
                    fit_rect,
                    translated,
                    fontname=cell_alias,
                    fontfile=str(cell_font),
                    fontsize=fitted_sizes[cell.id],
                    align=_cell_alignment(align, cell),
                    overlay=True,
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
