"""MinerU 4 PDF translation service.

MinerU owns PDF parsing and ORIGINAL-layout rendering. PyMuPDF remains the
geometry/audit authority, while the existing translation providers own
bounded semantic batches and protected-token validation.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol

from .pdf_hybrid_parser import parse_pdf
from .pdf_pipeline import (
    PdfPreflight,
    PdfPreflightError,
    inspect_pdf,
    publish_candidate,
    repair_pdf_text_cmaps,
    validate_candidate,
    write_pdf_report,
)
from .pdf_layout import LayoutContractError, load_numbering_profile, restore_layout_contract
from document_translator.core import (
    DocumentFormat,
    DocumentLocation,
    TranslationResult,
    TranslationUnit,
    generate_unit_id,
    validate_result_for_unit,
)
from document_translator.translation_rules import localize_chinese_dates, rule_protected_tokens


class TranslationBatchProvider(Protocol):
    provider_name: str
    model: str
    prompt_version: str
    glossary_version: str

    def translate_batch(self, units: list[TranslationUnit]) -> list[TranslationResult]: ...


@dataclass(slots=True)
class _TextUnit:
    unit: TranslationUnit
    targets: tuple[tuple[dict[str, Any], str], ...]
    page: int
    block_type: str
    bbox: tuple[float, float, float, float] | None
    # Present only when `targets[1:]` are entries inside this same list (the
    # "pages/blocks" content-list shape). Those schema-bound TextSpan dicts
    # require non-empty content, so a merged-away span must be dropped from
    # here rather than blanked in place.
    content_list: list[dict[str, Any]] | None = None


class MinerUPdfTranslationService:
    """Translate a PDF through MinerU's structured document model."""

    def __init__(self, provider: TranslationBatchProvider, *, tier: str = "flash") -> None:
        if tier not in {"flash", "basic", "standard", "advanced"}:
            raise ValueError("MinerU tier must be flash, basic, standard, or advanced")
        self.provider = provider
        self.tier = tier
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
        if preflight.classification == "E" or preflight.classification == "F":
            raise PdfPreflightError(
                f"PDF class {preflight.classification} is not eligible for MinerU translation: "
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

        try:
            from mineru.parser import parse
            from mineru.render import PdfLayout, render_pdf
            from docvortex.schema import MiddleJson
        except ImportError as exc:  # pragma: no cover - deployment dependent
            raise RuntimeError(
                "MinerU 4 is not installed in the active environment; install the base "
                'package with: uv pip install "mineru>=4.0,<5"'
            ) from exc

        source_manifest = parse_pdf(source, parser="native")
        profile = load_numbering_profile(style_profile, target_language=target_language)
        ocr_mode = "ocr" if preflight.classification == "D" else "txt"
        run: dict[str, object] = {
            "engine": "mineru4",
            "mineru_tier": self.tier,
            "mineru_ocr_mode": ocr_mode,
            "source_hash": preflight.source_hash,
            "hybrid_parse": {
                "parser": source_manifest.parser,
                "native_line_count": len(source_manifest.native_lines),
                "semantic_block_count": len(source_manifest.semantic_blocks),
                "rotated_pages": list(source_manifest.rotated_pages),
                "table_pages": list(source_manifest.table_pages),
            },
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="document-translator-mineru-", dir=destination.parent) as workdir_text:
            workdir = Path(workdir_text)
            candidate = workdir / "mineru-original-candidate.pdf"
            try:
                result = parse(str(source), tier=self.tier, ocr_mode=ocr_mode)
                middle_json = _middle_json(result)
                units = _extract_text_units(
                    middle_json,
                    source_hash=preflight.source_hash,
                    source_language=source_language,
                    target_language=target_language,
                )
                translations, translation_warnings = _translate_units(self.provider, units)
                if translation_warnings:
                    translations, translation_warnings = _remediate_warnings(
                        self.provider,
                        {item.unit.id: item.unit for item in units},
                        translations,
                        translation_warnings,
                    )
                for item in units:
                    _apply_translation(item, translations[item.unit.id])
                render_target = MiddleJson.from_dict(middle_json) if isinstance(middle_json, dict) else middle_json
                pdf_bytes = render_pdf(render_target, layout=PdfLayout.ORIGINAL)
                candidate.write_bytes(bytes(pdf_bytes))
                run["layout_restore"] = _restore_layout(source, candidate, target_language=target_language, profile=profile, minimum_font_size=minimum_font_size)
                run["restored_images"] = _restore_missing_images(source, candidate, middle_json)
                run["table_translation"] = _translate_tables(
                    self.provider,
                    source,
                    candidate,
                    source_hash=preflight.source_hash,
                    source_language=source_language,
                    target_language=target_language,
                    minimum_font_size=minimum_font_size,
                    allowed_pages=source_manifest.table_pages,
                    geometry_source=source,
                )
                run["restored_table_images"] = _restore_orphaned_table_images(source, candidate)
                # A page whose table was already rendered by the geometry-aware
                # cell path above must not be handed to the native vector-page
                # fallback below: that fallback replaces the ENTIRE page with
                # its own, independently-redacted rendering, which has no
                # notion of the hyperlink restoration or decorative-underline
                # cleanup pdf_table.py's cell renderer already performed for
                # this page. Without this exclusion, a page whose MinerU-drawn
                # vector table grid also happens to trip the vector-heavy or
                # untranslated-page heuristics below gets its already-correct
                # table silently clobbered: any hyperlink on it is dropped and
                # its now-orphaned underline decoration is left as a stray
                # line crossing the reflowed text (observed: a table cell's
                # link swallowed and its underline left running through an
                # unrelated line beneath it).
                table_patched_pages = set(run["table_translation"].get("patched_pages", ()))
                vector_heavy_pages = sorted(
                    (set(_find_vector_heavy_pages(source, candidate))
                     | set(_find_untranslated_pages(candidate, target_language=target_language)))
                    - table_patched_pages
                )
                if vector_heavy_pages:
                    run["vector_page_translation"] = _translate_vector_pages_natively(
                        self.provider,
                        source,
                        candidate,
                        vector_heavy_pages,
                        source_hash=preflight.source_hash,
                        source_language=source_language,
                        target_language=target_language,
                        minimum_font_size=minimum_font_size,
                    )
                run["restored_vector_pages"] = _restore_missing_vector_pages(
                    source, candidate, skip_pages=table_patched_pages
                )
                run["mineru_unit_count"] = len(units)
                run["translated_unit_count"] = len(translations)
                run["translation_warning_count"] = len(translation_warnings)
                if translation_warnings:
                    run["translation_warnings"] = translation_warnings
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
                published = publish_candidate(candidate, destination)
                return published, preflight, report
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


def _restore_layout(
    source: Path,
    candidate: Path,
    *,
    target_language: str,
    profile: object,
    minimum_font_size: float,
) -> dict[str, object]:
    """Reflow headings, centered text and references onto the source contract.

    restore_layout_contract() already implements this project's font-size
    and alignment policy (prefer the source size, only shrink on real
    overflow; keep the source line count and alignment) but MinerU's own
    renderer does not follow it -- it was written but never wired into this
    pipeline. The call is best-effort: a contract that cannot be matched to
    the translated candidate (translation phrasing can shift enough to
    break the match) is recorded as a skipped warning instead of failing
    the whole document, consistent with the rest of this pipeline.
    """
    repaired = candidate.with_name(candidate.stem + ".layout-restored" + candidate.suffix)
    try:
        summary = restore_layout_contract(
            source,
            candidate,
            repaired,
            target_language=target_language,
            profile=profile,
            minimum_font_size=minimum_font_size,
        )
    except LayoutContractError as exc:
        return {"status": "skipped", "reason": str(exc)}
    repaired.replace(candidate)
    return summary


def _clear_placeholder_text(page: Any, rect: Any) -> None:
    """Remove any text already drawn inside ``rect`` before an image is pasted there.

    MinerU's own render_pdf() step draws a literal "image unavailable"
    placeholder string wherever it could not populate an image block's own
    payload -- confirmed directly, still present in the candidate's text
    layer at the exact position a restored image is then painted over. The
    image hides it visually, but the stray text remains selectable and
    was observed to throw off two separate downstream checks that read
    the page's own text content: pdf_layout.py's contract matcher can pick
    it as the "closest" block for an unrelated centered-text role, and the
    font-size hard gate compares its own (irrelevant) size against that
    role's real source size. Redacting first removes the placeholder
    outright; ``graphics=0``/``images=0`` keep everything else on the page
    untouched, matching the same redaction shape used throughout this
    project's own table renderer.
    """
    page.add_redact_annot(rect, fill=None)
    page.apply_redactions(images=0, graphics=0, text=0)


def _insert_source_image(
    source_page: Any,
    candidate_page: Any,
    source_rect: Any,
    dest_rect: Any = None,
    *,
    overlay: bool = True,
) -> None:
    """Copy one source image into the candidate as the ORIGINAL image object.

    Embeds the source's own image bytes and soft mask unchanged, rather than
    rasterizing ``source_rect`` off the rendered source page: a region
    snapshot captures everything visible in that rectangle, not just the
    image -- confirmed directly, a company seal stamped over its signature
    line came back with the source's black Chinese company name and date
    baked into its pixels, its transparency mask flattened away, and the
    result no longer the independently deletable seal the source had.

    ``dest_rect`` defaults to the source image's own exact bbox (a
    same-position restore). The region snapshot remains only as a fallback
    for content that has no matching raster object on the source page at
    all (vector artwork), or whose matching image is rotated/flipped and
    so cannot be placed by an axis-aligned rect.
    """
    import fitz

    source_rect = fitz.Rect(source_rect)
    match = None
    for block in source_page.get_text("dict", flags=fitz.TEXT_PRESERVE_IMAGES).get("blocks", []):
        if block.get("type") != 1 or not block.get("image"):
            continue
        block_rect = fitz.Rect(block["bbox"])
        overlap = (block_rect & source_rect).get_area()
        if overlap < 0.6 * source_rect.get_area() or overlap < 0.6 * block_rect.get_area():
            continue
        a, b, c, d = (block.get("transform") or (1, 0, 0, 1, 0, 0))[:4]
        if abs(b) > 1e-6 or abs(c) > 1e-6 or a <= 0 or d <= 0:
            continue
        match = block
        break
    if match is not None:
        try:
            candidate_page.insert_image(
                fitz.Rect(dest_rect) if dest_rect is not None else fitz.Rect(match["bbox"]),
                stream=match["image"],
                mask=match.get("mask") or None,
                overlay=overlay,
            )
            return
        except Exception:
            pass
    pixmap = source_page.get_pixmap(clip=source_rect, dpi=200)
    candidate_page.insert_image(
        fitz.Rect(dest_rect) if dest_rect is not None else source_rect, pixmap=pixmap, overlay=overlay
    )


def _restore_missing_images(source: Path, candidate: Path, middle_json: dict[str, Any]) -> int:
    """Copy each image straight from the untouched source page.

    MinerU's crop-and-attach step does not reliably populate an image
    payload for every image-type block here (observed: an empty image_body
    with no image_base64/image_path/image_url at all), even though the
    block's bbox is correct. Only the text needs reconstructing, so take
    the image from the source page directly instead of depending on that
    extraction, and stamp it into the candidate at the identical position.
    """
    import fitz

    pages = middle_json.get("pages")
    if not isinstance(pages, list):
        pages = []
    restored = 0
    source_doc = fitz.open(source)
    candidate_doc = fitz.open(candidate)
    try:
        for page_index, page in enumerate(pages):
            if not isinstance(page, dict):
                continue
            if page_index >= source_doc.page_count or page_index >= candidate_doc.page_count:
                continue
            source_page = source_doc[page_index]
            candidate_page = candidate_doc[page_index]
            width, height = source_page.rect.width, source_page.rect.height
            blocks = page.get("blocks")
            if not isinstance(blocks, list):
                continue
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "image":
                    continue
                bbox = _bbox(block.get("bbox"))
                if bbox is None:
                    continue
                x0, y0, x1, y1 = bbox
                rect = fitz.Rect(x0 * width, y0 * height, x1 * width, y1 * height)
                if rect.width <= 1 or rect.height <= 1:
                    continue
                _clear_placeholder_text(candidate_page, rect)
                _insert_source_image(source_page, candidate_page, rect)
                restored += 1

        # A small diagram embedded INSIDE a table cell (observed: a single-
        # line diagram schematic sitting in a table's "description" column)
        # is never even surfaced as its own block -- MinerU folds the whole
        # cell into the surrounding table block's text content and has no
        # slot for a non-text element inside it, so it is dropped with no
        # image-type block to trigger the pass above at all. That case is
        # handled separately by _restore_orphaned_table_images() below,
        # AFTER table translation has settled the candidate's final table
        # geometry -- attempting it here, before translation, would place
        # the image against English-layout row heights that are about to
        # change once the table is translated.
        if restored:
            repaired = candidate.with_name(candidate.stem + ".with-images" + candidate.suffix)
            candidate_doc.save(str(repaired))
            candidate_doc.close()
            repaired.replace(candidate)
            candidate_doc = None
    finally:
        source_doc.close()
        if candidate_doc is not None:
            candidate_doc.close()
    return restored


def _lines_strict_table(page: Any) -> Any | None:
    """Return the page's first vector-line table, or ``None`` if it has none.

    Uses the same ``lines_strict`` strategy pdf_table.py's own extraction
    already relies on elsewhere in this project -- a table detected this
    way is read directly off the page's own drawn grid lines, not guessed.
    """
    tables = page.find_tables(strategy="lines_strict").tables
    return tables[0] if tables else None


def _cell_index_for_rect(table: Any, rect: Any) -> tuple[int, int] | None:
    """Return the (row, column) 0-based index of the cell containing ``rect``'s center."""
    import fitz

    center = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
    for row_index, row in enumerate(table.rows):
        for col_index, cell in enumerate(row.cells):
            if cell is None:
                continue
            if fitz.Rect(cell) is not None and center in fitz.Rect(cell):
                return row_index, col_index
    return None


def _rendered_text_bottom(page: Any, rect: Any) -> float:
    """Return the lowest y-coordinate any rendered glyph inside ``rect`` reaches.

    Falls back to ``rect.y0`` (an empty cell) when nothing is drawn there.
    """
    bottom = rect.y0
    for block in page.get_text("dict", clip=rect).get("blocks", ()):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", ()):
            for span in line.get("spans", ()):
                bottom = max(bottom, float(span["bbox"][3]))
    return bottom


def _nearest_content_top_below(page: Any, x0: float, x1: float, y_start: float) -> float:
    """Return the y-coordinate of whatever sits closest below ``y_start`` in that column span.

    Used as a safety ceiling: growing a table row must not push its new,
    lower bottom edge into a footer or any other real content already on
    the page below it. Returns the page's own bottom edge when nothing is
    found, meaning the whole remaining page height is free to use.
    """
    import fitz

    nearest = float(page.rect.height)
    band = fitz.Rect(x0, y_start, x1, page.rect.height)
    for block in page.get_text("dict", clip=band).get("blocks", ()):
        for line in block.get("lines", ()) if block.get("type") == 0 else ():
            for span in line.get("spans", ()):
                if span["bbox"][1] > y_start + 0.5:
                    nearest = min(nearest, float(span["bbox"][1]))
    for info in page.get_image_info():
        top = float(info["bbox"][1])
        if top > y_start + 0.5:
            nearest = min(nearest, top)
    return nearest


def _sample_grid_line_style(page: Any, table_rect: Any) -> tuple[tuple[float, float, float], float] | None:
    """Read this table's own border color/width straight off one of its lines.

    Never hardcode a style -- a table's grid can use any color or weight,
    and the only way to redraw a line that reads as "the same grid" is to
    copy it from a line the table itself already draws.
    """
    for drawing in page.get_drawings():
        if drawing.get("type") != "s" or drawing.get("color") is None:
            continue
        rect = drawing["rect"]
        if rect.width < 1 and table_rect.y0 - 1 <= rect.y0 and rect.y1 <= table_rect.y1 + 1:
            return tuple(drawing["color"]), float(drawing.get("width") or 0.5)
        if rect.height < 1 and table_rect.x0 - 1 <= rect.x0 and rect.x1 <= table_rect.x1 + 1:
            return tuple(drawing["color"]), float(drawing.get("width") or 0.5)
    return None


def _estimate_required_row_height(
    text: str, *, fontfile: str, fontname: str, fontsize: float, max_width: float
) -> float:
    """Return the cell height ``text`` actually needs at ``fontsize`` and ``max_width``.

    Measured with a real probe insert_textbox() call at successively taller
    heights, the exact same primitive pdf_table._fit_textbox() itself uses
    to decide whether a size fits -- an independent estimate (a line-count
    formula built on pdf_table._wrap_atomic_phrases()'s OWN width
    measurement) was tried first and was confirmed wrong on a real overflow:
    it predicted the text would comfortably fit in 2 lines while the real
    renderer's own probe rejected it even at the floor size, because
    _wrap_atomic_phrases() measures width with fitz.Font.text_length() while
    insert_textbox() wraps with its own internal metrics, and the two do not
    agree closely enough for this to be safe as a standalone estimate.
    Reusing insert_textbox() itself removes that gap entirely.

    ``text`` is run through pdf_table._normalise_render_text() first, the
    same call render_table_translations() itself makes before ever wrapping
    a cell's text -- skipping it here reproduced the exact same class of
    bug a second time: a numbered list ("1. ...; 2. ...; 3. ...") reflows
    as one dense paragraph without it, but _normalise_render_text() breaks
    each numbered item onto its own line first, which cannot reflow back
    together and so needs far more vertical room. Estimating against the
    un-normalised text (confirmed directly: a real overflowing list cell)
    silently underestimated the requirement, so the row was never grown and
    the real render still failed the exact same way afterwards.
    """
    import fitz

    from . import pdf_table

    normalised = pdf_table._normalise_render_text(text)
    wrapped = pdf_table._wrap_atomic_phrases(
        normalised, fontfile=fontfile, fontname=fontname, fontsize=fontsize, max_width=max_width
    )
    probe = fitz.open()
    try:
        page = probe.new_page(width=max_width + 40, height=2000)
        height = fontsize * 1.35 * 2
        for _ in range(60):
            result = page.insert_textbox(
                fitz.Rect(0, 0, max_width, height),
                wrapped,
                fontname=fontname,
                fontfile=fontfile,
                fontsize=fontsize,
                overlay=True,
            )
            if result >= -1e-6:
                return height
            height *= 1.25
        return height
    finally:
        probe.close()


def _recover_table_fit_error(
    candidate: Path,
    good_tables: list[Any],
    good_cell_translations: dict[str, str],
    cell_id: str | None,
    *,
    table_cell_font: Any,
    minimum_font_size: float,
    growth_target_size: float | None = None,
) -> tuple[int, int, float] | None:
    """Grow the one row that overflowed even at the floor font size, in place.

    A translation into a language that runs wider than the source (English
    expanding a Chinese table, observed directly: a 5-item numbered list in
    one cell no longer fit at any legible size within its Chinese-sized row)
    used to fail the WHOLE table over this single cell. Growing that row
    reuses the exact same mechanism already used to make room for an image
    restored into a cell -- redraw the grid, push later rows in the same
    table down -- so the fix is a row-height problem here too, not a font
    problem. Returns ``(page_number, zero_based_row_index, deficit)`` only
    if the row was actually grown (leaving the candidate file modified in
    place); the caller must shift every affected cell's OWN in-memory rect
    by that same amount rather than re-extracting tables from the now-
    modified page (see the caller for why). ``None`` means nothing changed
    and the caller's existing fail-closed behaviour applies unchanged.

    ``growth_target_size``, when given and larger than ``minimum_font_size``,
    is tried FIRST, stepping down toward the floor only as far as the page's
    remaining room actually demands -- one real _grow_table_row() attempt
    per size, not a pre-check. Estimating only ever against the bare floor
    (the original behaviour, still used when this is omitted) grows a row
    by just enough for the smallest, least readable size, so even a cell
    with plenty of page space left to grow into never ends up any larger
    than the floor -- confirmed directly on a real table: every row that
    needed growing rendered between 6-8.5pt against a 12pt source even
    though the page below the table was still mostly empty. The caller is
    responsible for choosing a ``growth_target_size`` the table as a whole
    can actually afford (see _translate_tables's own quality sweep): a
    cell-by-cell "always aim high" policy risks spending page space one
    early row didn't need to leave nothing for a later row that did,
    turning a table that would have fit at the floor into one that fits at
    no size at all.
    """
    if not cell_id:
        return None
    from . import pdf_table

    target_cell = None
    for table in good_tables:
        for cell in table.cells:
            if cell.id == cell_id:
                target_cell = cell
                break
        if target_cell is not None:
            break
    if target_cell is None or target_cell.rect is None:
        return None
    translated_text = good_cell_translations.get(cell_id)
    if not translated_text:
        return None

    import fitz

    font_path = str(table_cell_font(target_cell, translated_text))
    cell_rect = fitz.Rect(target_cell.rect)
    preferred_size = max(growth_target_size or minimum_font_size, minimum_font_size)

    size = preferred_size
    while size >= minimum_font_size - 1e-9:
        required = _estimate_required_row_height(
            translated_text,
            fontfile=font_path,
            fontname="probe",
            fontsize=size,
            max_width=cell_rect.width - 4.0,
        )
        deficit = required + 4.0 - cell_rect.height
        if deficit > 0.5:
            candidate_doc = fitz.open(candidate)
            try:
                page = candidate_doc[target_cell.page_number - 1]
                table = _lines_strict_table(page)
                row_index = target_cell.row - 1
                grown = None if table is None else _grow_table_row(page, table, row_index, deficit)
                if grown is not None:
                    repaired = candidate.with_name(candidate.stem + ".row-grown" + candidate.suffix)
                    candidate_doc.save(str(repaired))
                    candidate_doc.close()
                    repaired.replace(candidate)
                    candidate_doc = None
                    return (target_cell.page_number, row_index, deficit)
            finally:
                if candidate_doc is not None:
                    candidate_doc.close()
        if size <= minimum_font_size + 1e-9:
            break
        size = max(minimum_font_size, size - 1.0)
    return None


def _shift_table_geometry_after_growth(
    good_tables: list[Any], page_number: int, row_index: int, deficit: float
) -> list[Any]:
    """Relocate cell/table rects after a row grew, WITHOUT re-reading the page.

    _grow_table_row() rasterizes everything below the grown row into a
    single pasted image so the shift is visually seamless -- fine for its
    original purpose (making room for an already-placed image), but fatal
    to re-extracting table geometry afterwards via
    pdf_table.extract_pdf_tables(): every cell in that rasterized band
    loses its real text layer, so PdfTableCell.is_empty (a pure function of
    the text captured at extraction time) flips to True for cells that
    still have a real, pending translation -- confirmed directly: growing a
    SECOND overflowing row below an already-grown one raised "empty source
    cell ... cannot receive non-empty translation" for a label cell that
    plainly was not empty moments before. Recomputing each cell's rect in
    memory instead -- grow the target row's own height, shift every row
    below it down by the same amount, leave rows above untouched -- keeps
    each PdfTableCell's original ``text`` (and therefore its ``is_empty``)
    intact while still reflecting the page's new, real geometry.
    """
    import dataclasses

    updated_tables = []
    for table in good_tables:
        if table.page_number != page_number:
            updated_tables.append(table)
            continue
        new_cells = []
        for cell in table.cells:
            if cell.rect is None or cell.row - 1 < row_index:
                new_cells.append(cell)
                continue
            x0, y0, x1, y1 = cell.rect
            if cell.row - 1 == row_index:
                new_rect = (x0, y0, x1, y1 + deficit)
            else:
                new_rect = (x0, y0 + deficit, x1, y1 + deficit)
            new_cells.append(dataclasses.replace(cell, rect=new_rect))
        tx0, ty0, tx1, ty1 = table.rect
        updated_tables.append(dataclasses.replace(table, rect=(tx0, ty0, tx1, ty1 + deficit), cells=tuple(new_cells)))
    return updated_tables


def _strip_growth_artifact_images(candidate: Path, page_number: int, table_rect: tuple[float, float, float, float]) -> None:
    """Remove any image _grow_table_row() has pasted within this table, in place.

    Every "move content below down" step in _grow_table_row() pastes a
    fresh raster snapshot of whatever it just shifted; growing a LATER row
    in the same table correctly clears the previous snapshot before
    pasting its own (see _grow_table_row's own vertical-fragment comment),
    but a row that never becomes a growth target itself -- the last one or
    two rows in a table, once the recovery attempt cap is reached -- stays
    inside whatever snapshot last swept over it. This pipeline's table-cell
    text redaction preserves images on purpose (a legitimately restored
    cell image is meant to survive it), so that stray snapshot would
    otherwise still be sitting there when the real translated text is
    drawn on top of it moments later -- confirmed directly: the bottom two
    rows of a real table stayed visibly double-exposed, source text
    showing right through the correctly translated text above it, even
    though every row above rendered cleanly.

    Called right after every successful growth, before the next render
    attempt, so render_table_translations() -- which refuses to publish a
    page with FEWER images than its input had, a guard meant to catch an
    accidentally deleted real image -- measures its own "before" count
    from a candidate that no longer carries this transient artifact, and
    never needs to remove it (or trip that guard) itself.
    """
    import fitz

    doc = fitz.open(candidate)
    try:
        page = doc[page_number - 1]
        page.add_redact_annot(fitz.Rect(table_rect), fill=None)
        page.apply_redactions(images=1, graphics=0, text=1)
        repaired = candidate.with_name(candidate.stem + ".images-stripped" + candidate.suffix)
        doc.save(str(repaired))
        doc.close()
        repaired.replace(candidate)
        doc = None
    finally:
        if doc is not None:
            doc.close()


def _grow_table_row(page: Any, table: Any, row_index: int, deficit: float) -> Any | None:
    """Grow one real table row by ``deficit`` points, pushing later rows down to match.

    Only ever resizes a row that is already there with real content --
    never adds a row or cell. The table's grid must be the simple
    straight-line style this project's own tables use (every vertical
    divider spans the table's full height, every row boundary is one
    full-width horizontal line); anything else, or too little clear page
    space below the table to grow into, aborts and returns ``None`` so the
    caller can fall back to leaving the geometry untouched.
    """
    import fitz

    if deficit <= 0:
        return None
    rows = table.rows
    if row_index < 0 or row_index >= len(rows):
        return None
    row_rect = fitz.Rect(rows[row_index].bbox)
    table_rect = fitz.Rect(table.bbox)
    old_bottom = table_rect.y1
    new_bottom = old_bottom + deficit

    ceiling = _nearest_content_top_below(page, table_rect.x0, table_rect.x1, old_bottom)
    if new_bottom + 6.0 > ceiling:
        return None

    style = _sample_grid_line_style(page, table_rect)
    if style is None:
        return None
    color, width = style

    drawings = page.get_drawings()
    # A vertical divider that has already been grown once (by an earlier
    # call, for a different overflowing row in the same table) is no
    # longer one continuous stroke from the table's top to its bottom --
    # the previous growth drew a SEPARATE extension segment rather than
    # replacing it, so what reaches this table's current bottom is really
    # two (or more) stacked fragments at the same x. Requiring a single
    # drawing that spans the full table_rect.y0-to-old_bottom range (the
    # original assumption here) matches nothing once that has happened --
    # confirmed directly: growing a second row after a first successful
    # growth found zero verticals and silently gave up. Group by x instead
    # and extend from whatever each column divider's current lowest point
    # actually is.
    verticals_by_x: dict[float, float] = {}
    for d in drawings:
        rect = d["rect"]
        if d.get("type") == "s" and rect.width < 1 and abs(rect.y0 - table_rect.y0) < 1.5:
            x = round(float(rect.x0), 2)
            verticals_by_x[x] = max(verticals_by_x.get(x, rect.y1), float(rect.y1))
    horizontals_to_shift = [
        d for d in drawings
        if d.get("type") == "s" and d["rect"].height < 1
        and d["rect"].y0 >= row_rect.y1 - 1.5
        and d["rect"].x0 >= table_rect.x0 - 1.5 and d["rect"].x1 <= table_rect.x1 + 1.5
    ]
    if not verticals_by_x or not horizontals_to_shift:
        return None

    # Move whatever already renders strictly below this row (later rows'
    # own real translated text, within this table's own column span) down
    # by the same amount, as a single rasterized strip -- simpler and far
    # less error-prone than re-deriving every downstream cell's exact font
    # and re-inserting its text, and it only costs text-selectability for
    # that thin trailing band, not the row that actually gains the image.
    below_band = fitz.Rect(table_rect.x0, row_rect.y1, table_rect.x1, old_bottom)
    moved_pixmap = None
    if below_band.height > 0.5:
        moved_pixmap = page.get_pixmap(clip=below_band, dpi=200)
        page.add_redact_annot(below_band, fill=None)
        # images=1 (remove any overlapping image), not the original 0
        # (ignore/preserve): growing a SECOND overflowing row in the same
        # table re-captures and re-pastes this same band, which by then
        # already contains the FIRST growth's own pasted snapshot -- with
        # images preserved that old snapshot is never cleared from its own
        # (now stale) position, so it keeps showing there as a duplicate
        # underneath the fresh snapshot pasted at the newly shifted spot.
        # Confirmed directly: growing every overflowing row in a 10-row
        # table one at a time left visibly doubled/ghosted text across
        # every row below the first growth. Text redaction was already
        # correct (0 = remove); only the image mode needed to match it.
        page.apply_redactions(images=1, graphics=0, text=0)

    # apply_redactions(graphics=...) removes a WHOLE vector path the instant
    # any part of it is touched by the redaction box -- not just the
    # covered slice -- so a redaction box spanning the table's full width
    # or full height collaterally deletes every perpendicular grid line it
    # crosses along the way, not only the one line actually being
    # relocated. Split each horizontal's redaction into one narrow box per
    # column gap, stopping just short of every vertical divider's own x
    # position, so no box ever touches a vertical line at all. A zero-
    # height stroke's rect also has no area for the match itself, hence
    # the small inflation on the thin axis.
    vertical_xs = sorted(verticals_by_x.keys())
    gap = 0.9
    for drawing in horizontals_to_shift:
        r = drawing["rect"]
        for left, right in zip(vertical_xs, vertical_xs[1:]):
            if right - left <= 2 * gap:
                continue
            box = fitz.Rect(left + gap, r.y0 - 0.6, right - gap, r.y1 + 0.6)
            page.add_redact_annot(box, fill=None)
    if horizontals_to_shift:
        page.apply_redactions(images=0, graphics=2, text=0)

    for drawing in horizontals_to_shift:
        r = drawing["rect"]
        new_y = r.y0 + deficit
        page.draw_line((r.x0, new_y), (r.x1, new_y), color=color, width=width)
    for x, current_bottom in verticals_by_x.items():
        page.draw_line((x, current_bottom), (x, new_bottom), color=color, width=width)

    if moved_pixmap is not None:
        dest = fitz.Rect(below_band.x0, below_band.y0 + deficit, below_band.x1, below_band.y1 + deficit)
        page.insert_image(dest, pixmap=moved_pixmap)

    return fitz.Rect(row_rect.x0, row_rect.y0, row_rect.x1, row_rect.y1 + deficit)


def _place_image_in_table_cell(source_page: Any, candidate_page: Any, image_rect: Any) -> bool:
    """Try to restore ``image_rect`` inside the real table cell it belongs to.

    Looks up which row/column of the SOURCE page's own vector table the
    image sits in, then maps that row/column onto the CANDIDATE page's
    copy of the same table -- both read from each PDF's own real grid
    lines, never invented. If the two tables don't even agree on a shape,
    or the image's own row can't be resized safely, this backs out
    (returns False) and leaves the caller's plain same-position paste as
    the fallback, exactly as before this cell-aware placement existed.
    """
    import fitz

    source_table = _lines_strict_table(source_page)
    candidate_table = _lines_strict_table(candidate_page)
    if source_table is None or candidate_table is None:
        return False
    if len(source_table.rows) != len(candidate_table.rows) or source_table.col_count != candidate_table.col_count:
        return False
    cell_index = _cell_index_for_rect(source_table, image_rect)
    if cell_index is None:
        return False
    row_index, col_index = cell_index
    candidate_row = candidate_table.rows[row_index]
    if col_index >= len(candidate_row.cells) or candidate_row.cells[col_index] is None:
        return False
    cell_rect = fitz.Rect(candidate_row.cells[col_index])

    padding = 3.0
    text_bottom = _rendered_text_bottom(candidate_page, cell_rect)
    available_top = text_bottom + padding
    target_width = cell_rect.width - 2 * padding
    if target_width <= 5:
        return False
    scale = target_width / image_rect.width
    target_height = image_rect.height * scale
    if target_height > cell_rect.height - padding:
        scale = max((cell_rect.height - padding) / image_rect.height, 0.0)
        target_height = image_rect.height * scale
        target_width = image_rect.width * scale
    if target_width <= 5 or target_height <= 5:
        return False

    slack = cell_rect.y1 - available_top
    deficit = (target_height + padding) - slack
    if deficit > 0.5:
        grown_rect = _grow_table_row(candidate_page, candidate_table, row_index, deficit)
        if grown_rect is None:
            return False
        cell_rect = fitz.Rect(cell_rect.x0, cell_rect.y0, cell_rect.x1, grown_rect.y1)

    dest = fitz.Rect(
        cell_rect.x0 + (cell_rect.width - target_width) / 2,
        available_top,
        cell_rect.x0 + (cell_rect.width - target_width) / 2 + target_width,
        available_top + target_height,
    )
    _insert_source_image(source_page, candidate_page, image_rect, dest)
    return True


def _restore_orphaned_table_images(source: Path, candidate: Path) -> int:
    """Restore any source image the translated candidate lost, cell-aware.

    Runs after table translation, once the candidate's tables are in their
    FINAL translated geometry -- a diagram embedded inside a table cell
    that MinerU folded into the surrounding text and dropped (never
    surfaced as its own image block, so _restore_missing_images()'s
    block-based pass never sees it) can end up outside its own table
    entirely once the translated text reflows that table shorter than the
    source. _place_image_in_table_cell() above tries to put it back inside
    the real cell it came from; anything it cannot place safely still gets
    the plain same-position paste this project has always used, so no
    image is ever silently left missing.
    """
    import fitz

    source_doc = fitz.open(source)
    candidate_doc = fitz.open(candidate)
    restored = 0
    try:
        for page_index in range(min(source_doc.page_count, candidate_doc.page_count)):
            source_page = source_doc[page_index]
            candidate_page = candidate_doc[page_index]
            candidate_rects = [fitz.Rect(info["bbox"]) for info in candidate_page.get_image_info()]
            for info in source_page.get_image_info():
                rect = fitz.Rect(info["bbox"])
                if rect.width <= 1 or rect.height <= 1:
                    continue
                covered = any((rect & other).get_area() >= 0.5 * rect.get_area() for other in candidate_rects)
                if covered:
                    continue
                if _place_image_in_table_cell(source_page, candidate_page, rect):
                    restored += 1
                    continue
                # overlay=False (background): this restores an image onto a
                # page whose translated text is already drawn, so a
                # foreground paste would draw the image over that text
                # wherever they overlap (a company chop stamped over its
                # signature line, by design). Background keeps the image in
                # its source position with the translated text on top.
                _insert_source_image(source_page, candidate_page, rect, overlay=False)
                restored += 1
        if restored:
            repaired = candidate.with_name(candidate.stem + ".with-table-images" + candidate.suffix)
            candidate_doc.save(str(repaired))
            candidate_doc.close()
            repaired.replace(candidate)
            candidate_doc = None
    finally:
        source_doc.close()
        if candidate_doc is not None:
            candidate_doc.close()
    return restored


def _find_vector_heavy_pages(
    source: Path,
    candidate: Path,
    *,
    minimum_source_drawings: int = 50,
    maximum_candidate_ratio: float = 0.1,
) -> list[int]:
    """Return 1-indexed pages whose source vector drawing was dropped.

    Shared by the native in-place translation attempt below and the
    raster fallback that follows it, so both apply the exact same
    "this page lost its drawing" test.
    """
    import fitz

    source_doc = fitz.open(source)
    candidate_doc = fitz.open(candidate)
    try:
        flagged: list[int] = []
        for page_index in range(min(source_doc.page_count, candidate_doc.page_count)):
            source_drawing_count = len(source_doc[page_index].get_drawings())
            if source_drawing_count < minimum_source_drawings:
                continue
            candidate_drawing_count = len(candidate_doc[page_index].get_drawings())
            if candidate_drawing_count > source_drawing_count * maximum_candidate_ratio:
                continue
            flagged.append(page_index + 1)
        return flagged
    finally:
        source_doc.close()
        candidate_doc.close()


def _find_untranslated_pages(
    candidate: Path,
    *,
    target_language: str,
    minimum_characters: int = 40,
) -> list[int]:
    """Return 1-indexed pages whose candidate text carries no target-script glyph.

    _find_vector_heavy_pages() above only catches a page whose vector
    drawing was dropped outright; a page like a site-plan legend or a
    drawing's title-block table can keep its vector lines (so that check
    never trips) while MinerU still classifies its text as something
    other than an ordinary translatable paragraph -- table-typed blocks
    are deliberately skipped by _make_units_from_current_block(), and
    pdf_table.py's own geometry detector only recognises a dense
    row/column grid, not a handful of boxed labels -- so the text is
    never routed to any translation path and the page publishes with
    its original English untouched. Reuse the exact same page-number
    contract as _find_vector_heavy_pages() so both feed the same native
    fallback below: any page with enough real text to be worth a
    translation call, but not a single target-script character in it,
    almost certainly means translation never touched that page at all.
    """
    from document_translator.font_policy import contains_cjk

    if not target_language.lower().startswith("zh"):
        return []
    import fitz

    candidate_doc = fitz.open(candidate)
    try:
        flagged: list[int] = []
        for page_index in range(candidate_doc.page_count):
            text = candidate_doc[page_index].get_text()
            if len(text.strip()) < minimum_characters:
                continue
            if contains_cjk(text):
                continue
            flagged.append(page_index + 1)
        return flagged
    finally:
        candidate_doc.close()


def _try_table_translation(
    provider: TranslationBatchProvider,
    subset_source: Path,
    subset_dest: Path,
    *,
    source_hash: str,
    source_language: str,
    target_language: str,
    minimum_font_size: float,
    minimum_table_text_ratio: float = 0.5,
    maximum_single_cell_share: float = 0.4,
) -> bool:
    """Render a flagged page's table through the geometry-aware cell path.

    _translate_vector_pages_natively() below falls back to
    pdf_translation.PdfTranslationService for a page whose vector content
    MinerU dropped, but that service redacts and reinserts each detected
    text span at its OWN source position with no idea any of them share a
    table row -- correct for a page of scattered labels (a site-plan
    legend, say), but on a genuine dense data table it reproduces the
    table as a pile of independently-placed spans that overlap each other
    as soon as a translation wraps to a different line count than its
    English source (observed: a 68-row equipment schedule rendered as
    unreadable stacked text). pdf_table.py's cell renderer already solves
    exactly this -- wrap/shrink-to-fit per cell, one cell never reads
    into another's space -- so try it FIRST on this single-page subset
    and only fall through to the whole-page service when this page either
    has no real table (pdf_table.py's stricter geometry detector finds
    nothing) or is not table-dominated (the table covers under half the
    page's own text, e.g. a title-block table below a mostly non-tabular
    map) -- the whole-page path already handles that second case well and
    should keep doing so rather than losing everything outside the table.
    """
    from . import pdf_table
    import fitz

    try:
        tables = pdf_table.extract_pdf_tables(subset_source, merge_phantom_rows=False)
    except pdf_table.PdfTableError:
        return False
    if not tables:
        return False
    cell_lengths = [len(cell.text) for table in tables for cell in table.cells if not cell.is_empty]
    table_characters = sum(cell_lengths)
    if not cell_lengths or table_characters <= 0:
        return False
    # find_tables() can mistake a page's own border, a title-block frame,
    # and a handful of coincidentally-aligned label boxes for a real grid
    # on a page that has no table at all (observed: a site-plan map, "7
    # rows x 12 columns" spanning nearly the whole page) -- every one of
    # its "rows"/"columns" is fabricated except the one real cell that
    # swallows almost the entire page's actual text, since nothing genuinely
    # divides the rest. A real data table spreads content across many
    # comparably-sized cells (this project's own lift-station schedule:
    # its largest single cell held under 3% of the table's total text); a
    # single cell holding an outsized share is this same signature in
    # reverse and means the "table" is not real, no matter how well its
    # geometry otherwise satisfies the ratio check below.
    if max(cell_lengths) / table_characters > maximum_single_cell_share:
        return False
    page_doc = fitz.open(subset_source)
    try:
        page_characters = len(page_doc[0].get_text())
    finally:
        page_doc.close()
    if page_characters <= 0 or table_characters / page_characters < minimum_table_text_ratio:
        return False

    source_doc = fitz.open(subset_source)
    try:
        source_doc.save(subset_dest)
    finally:
        source_doc.close()
    try:
        result = _translate_tables(
            provider,
            subset_source,
            subset_dest,
            source_hash=source_hash,
            source_language=source_language,
            target_language=target_language,
            minimum_font_size=minimum_font_size,
            merge_phantom_rows=False,
        )
    except Exception:
        subset_dest.unlink(missing_ok=True)
        return False
    if result.get("status") not in {"patched", "partially_patched"}:
        subset_dest.unlink(missing_ok=True)
        return False
    return True


def _translate_vector_pages_natively(
    provider: TranslationBatchProvider,
    source: Path,
    candidate: Path,
    page_numbers: list[int],
    *,
    source_hash: str,
    source_language: str,
    target_language: str,
    minimum_font_size: float,
) -> dict[str, object]:
    """Translate flagged pages in place instead of falling back to a raster.

    _restore_missing_vector_pages() below guarantees a flagged page is
    never left blank, but its raster paste keeps the page's text in the
    original, untranslated English. _try_table_translation() above is
    tried first for a page whose content turns out to be a genuine data
    table; document_translator.services.pdf_translation is this
    project's older, page-preserving PDF path used the rest of the time:
    it redacts and reinserts only the text spans it actually finds on a
    page and never touches anything else, so a CAD/site-plan page's
    vector lines are never rebuilt and never at risk -- the same
    property MinerU's render_pdf(ORIGINAL) lacks for this content. Try
    it one flagged page at a time so one page's stricter-than-
    usual validation failure (any one unit's error fails that call)
    cannot also sink a neighbouring page that would otherwise have
    translated cleanly; on a page's failure, that one page is left for
    the raster fallback to still cover, in its original English rather
    than silently blank.
    """
    from .pdf_translation import PdfTranslationService
    import fitz

    workdir = candidate.parent
    patched: list[int] = []
    skipped: list[dict[str, object]] = []
    for page_number in page_numbers:
        subset_source = workdir / (candidate.stem + f".vector-source-p{page_number}.pdf")
        subset_dest = workdir / (candidate.stem + f".vector-translated-p{page_number}.pdf")
        source_doc = fitz.open(source)
        try:
            subset = fitz.open()
            subset.insert_pdf(source_doc, from_page=page_number - 1, to_page=page_number - 1)
            subset.save(subset_source)
            subset.close()
        finally:
            source_doc.close()

        table_patched = _try_table_translation(
            provider,
            subset_source,
            subset_dest,
            source_hash=source_hash,
            source_language=source_language,
            target_language=target_language,
            minimum_font_size=minimum_font_size,
        )
        if not table_patched:
            try:
                PdfTranslationService(provider).translate_file(
                    subset_source, subset_dest, source_language=source_language, target_language=target_language,
                )
            except Exception as exc:
                skipped.append({"page": page_number, "reason": str(exc)})
                subset_source.unlink(missing_ok=True)
                continue
        subset_source.unlink(missing_ok=True)

        try:
            candidate_doc = fitz.open(candidate)
            translated_doc = fitz.open(subset_dest)
            try:
                index = page_number - 1
                candidate_doc.delete_page(index)
                candidate_doc.insert_pdf(translated_doc, from_page=0, to_page=0, start_at=index)
                repaired = candidate.with_name(candidate.stem + f".vector-native-p{page_number}" + candidate.suffix)
                candidate_doc.save(str(repaired))
            finally:
                candidate_doc.close()
                translated_doc.close()
            repaired.replace(candidate)
            patched.append(page_number)
        finally:
            subset_dest.unlink(missing_ok=True)
    return {"status": "patched" if patched else "skipped", "translated_pages": patched, "skipped": skipped}


def _restore_missing_vector_pages(
    source: Path,
    candidate: Path,
    *,
    skip_pages: set[int] = frozenset(),
    minimum_source_drawings: int = 50,
    maximum_candidate_ratio: float = 0.1,
) -> dict[str, object]:
    """Paste a full source-page raster behind any page MinerU emptied out.

    render_pdf(ORIGINAL) only reconstructs blocks MinerU's layout model
    recognised as text, image, or table. A page that is mostly a CAD
    line drawing -- a wiring schematic, a site-plan layout -- has none
    of those; MinerU has no "unclassified vector content" block type to
    even flag as missing, so the whole drawing is silently dropped with
    no restoration path, unlike a missing image block (which
    _restore_missing_images() above already rasterizes from source).
    The result is a page that is almost entirely blank except for
    whatever an isolated table happened to survive on it.

    Detect this by comparing vector-drawing counts: a source page with
    substantial drawing content whose candidate page has almost none of
    it left has lost that drawing outright. Paste a full rasterised
    copy of the source page in as the BACKGROUND (``overlay=False``) so
    it fills every gap without covering whatever the candidate already
    has -- a correctly translated table patched into the same page,
    say. Restored content stays in its original English, the same
    trade-off already accepted for restored images: visible beats
    blank.

    ``skip_pages`` (page numbers the table-cell path already patched, per
    ``_translate_tables``'s own report) must be excluded from this
    drawing-count heuristic entirely: a table with one huge, mostly
    prose cell (observed: a bulleted "facilities" clause spanning nearly
    the whole page, with almost no interior grid lines of its own) can
    legitimately have a low vector-drawing count on a page that was
    ALREADY correctly translated -- pasting an untranslated full-page
    English raster behind it does not fill a gap here, it papers the
    entire page with the original English, which then visibly shows
    through wherever the (typically more compact) Chinese translation
    does not happen to cover it (observed: an unreadable mix of both
    languages on a page whose table was, before this raster paste, fully
    and correctly translated).
    """
    import fitz

    source_doc = fitz.open(source)
    candidate_doc = fitz.open(candidate)
    restored_pages: list[int] = []
    try:
        for page_index in range(min(source_doc.page_count, candidate_doc.page_count)):
            if (page_index + 1) in skip_pages:
                continue
            source_page = source_doc[page_index]
            source_drawing_count = len(source_page.get_drawings())
            if source_drawing_count < minimum_source_drawings:
                continue
            candidate_page = candidate_doc[page_index]
            candidate_drawing_count = len(candidate_page.get_drawings())
            if candidate_drawing_count > source_drawing_count * maximum_candidate_ratio:
                continue
            pixmap = source_page.get_pixmap(dpi=200)
            candidate_page.insert_image(candidate_page.rect, pixmap=pixmap, overlay=False)
            restored_pages.append(page_index + 1)
        if restored_pages:
            repaired = candidate.with_name(candidate.stem + ".with-vector-pages" + candidate.suffix)
            candidate_doc.save(str(repaired))
            candidate_doc.close()
            repaired.replace(candidate)
            candidate_doc = None
    finally:
        source_doc.close()
        if candidate_doc is not None:
            candidate_doc.close()
    return {"restored_page_count": len(restored_pages), "restored_pages": restored_pages}


def _table_cjk_fallback_font() -> str:
    """Prefer the cached Source Han Sans build already used elsewhere.

    A prior BabelDOC run cached this build locally as a translation asset;
    reuse it without adding a babeldoc dependency to this pipeline, and
    fall back to the bundled Windows SimHei build only if that cache is
    missing (matches pdf_translation.py's own fallback font choice).
    """
    cached = Path.home() / ".cache" / "babeldoc" / "fonts" / "SourceHanSansCN-Regular.ttf"
    if cached.is_file():
        return str(cached)
    return r"C:\Windows\Fonts\simhei.ttf"


def _translate_batch_with_retry(
    provider: TranslationBatchProvider,
    units: list[TranslationUnit],
    *,
    attempts: int = 5,
    initial_delay: float = 2.0,
) -> list[TranslationResult]:
    """Retry a batch call that failed transport-level, not content-level.

    provider.translate_batch() already returns a "needs_review" result
    instead of raising for a unit whose CONTENT fails validation (residual
    English, a dropped identifier, ...) -- callers handle that themselves.
    What still raises here is the provider failing to produce a usable
    response at all (observed: "DashScope Qwen Chat response was invalid",
    a malformed/truncated JSON body) -- generation variance, not a defect
    in the request: the identical 18-unit, 716-character batch that failed
    three immediate retries in a full document run went on to succeed on
    its very next attempt run in isolation seconds later, which reads as a
    brief server-side condition rather than something about that request
    -- an immediate retry can still land inside the same bad window,
    where a short, growing delay gives it room to clear. Both call sites
    below send many sequential batches across a real multi-page document
    (a 9-page drawing set's table cells alone can mean dozens of
    requests); without this, one bad response anywhere in that sequence
    discarded every already-completed batch and aborted the whole
    document. A non-transient failure (a bad API key, say) still raises
    after every attempt, just slower.
    """
    import time

    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            return provider.translate_batch(units)
        except Exception as exc:  # noqa: BLE001 - provider-agnostic by design; see docstring
            last_error = exc
            if attempt < attempts - 1:
                time.sleep(initial_delay * (2**attempt))
    assert last_error is not None
    raise last_error


def _table_cell_font(cell: object, translated: str) -> str:
    """Apply this project's Latin/CJK-aware font policy per table cell.

    A single fixed font for the whole table (the earlier version of this
    integration) draws an untranslated English identifier or place name
    left inside an otherwise-Chinese table with a CJK font file, instead
    of this project's established pdf_layout._font_file() selection
    (already used by the PyMuPDF-native fallback path in
    pdf_translation.py) -- switching the parsing engine from the deleted
    BabelDOC integration to MinerU must not silently drop that policy.
    _font_file() itself decides CJK vs Latin from the text; the page
    argument is omitted because a disposable per-cell probe page has no
    already-embedded fonts to avoid colliding with.
    """
    from .pdf_layout import _font_file
    from document_translator.font_policy import contains_cjk

    return _font_file("", translated, prefer_narrow=len(translated) >= 80) or (
        _table_cjk_fallback_font() if contains_cjk(translated) else r"C:\Windows\Fonts\arial.ttf"
    )


def _attempt_table_render(
    candidate: Path,
    patched: Path,
    original_candidate_bytes: bytes,
    tables: list[Any],
    good_tables: list[Any],
    good_cell_translations: dict[str, str],
    *,
    minimum_font_size: float,
    growth_target_size: float,
    source_page_links: dict[int, list[dict]],
    units: list[Any],
    warnings: list[dict[str, object]],
) -> tuple[dict[str, object], list[Any]]:
    """Try rendering the whole table once against a fresh copy of ``candidate``.

    One call is one quality level: every row that overflows is grown
    toward ``growth_target_size`` (stepping down toward the floor only as
    far as that specific row's own remaining room demands), then the
    reduced-floor retry this module has always had covers whatever still
    does not fit even there. Always starts from ``original_candidate_bytes``
    rather than whatever is currently on disk, so the caller can retry at a
    higher quality level without a previous attempt's growth leaving the
    page in a different state. Returns the same result-dict shape
    _translate_tables itself returns, plus the final good_tables geometry
    (grown or not) for a caller that wants it.
    """
    from . import pdf_table

    candidate.write_bytes(original_candidate_bytes)
    reduced_floor = round(max(4.5, minimum_font_size - 1.5), 2)
    floors = [minimum_font_size] if reduced_floor >= minimum_font_size else [minimum_font_size, reduced_floor]
    last_exc: Exception | None = None
    for floor in floors:
        attempts = 0
        while True:
            try:
                report = pdf_table.render_table_translations(
                    candidate,
                    patched,
                    good_cell_translations,
                    tables=good_tables,
                    fontfile=_table_cell_font,
                    minimum_font_size=floor,
                    page_links=source_page_links,
                )
            except pdf_table.PdfTableFitError as exc:
                patched.unlink(missing_ok=True)
                last_exc = exc
                attempts += 1
                grown = attempts <= 20 and _recover_table_fit_error(
                    candidate,
                    good_tables,
                    good_cell_translations,
                    getattr(exc, "cell_id", None),
                    table_cell_font=_table_cell_font,
                    minimum_font_size=floor,
                    growth_target_size=growth_target_size,
                )
                if grown:
                    page_number, row_index, deficit = grown
                    good_tables = _shift_table_geometry_after_growth(good_tables, page_number, row_index, deficit)
                    table_rect = next(
                        (t.rect for t in good_tables if t.page_number == page_number), None
                    )
                    if table_rect is not None:
                        _strip_growth_artifact_images(candidate, page_number, table_rect)
                    continue
                break
            except pdf_table.PdfTableError as exc:
                # fail closed, per this module's own contract: a cell that
                # cannot be rendered safely must not silently keep the
                # untranslated English rather than corrupt or overflow the
                # table, but the rest of the page (already rendered by
                # MinerU) is still worth publishing, so this is recorded
                # rather than raised.
                return (
                    {"status": "failed", "reason": str(exc), "table_count": len(good_tables), "cell_count": len(units), "warnings": warnings},
                    good_tables,
                )
            else:
                patched.replace(candidate)
                return (
                    {
                        "status": "patched" if len(good_tables) == len(tables) else "partially_patched",
                        "table_count": report.table_count,
                        "cell_count": report.cell_count,
                        "rendered_cell_count": report.rendered_cell_count,
                        "restored_link_count": report.restored_link_count,
                        "skipped_table_count": len(tables) - len(good_tables),
                        "patched_pages": sorted({table.page_number for table in good_tables}),
                        "warnings": warnings,
                    },
                    good_tables,
                )
    return (
        {"status": "failed", "reason": str(last_exc), "table_count": len(good_tables), "cell_count": len(units), "warnings": warnings},
        good_tables,
    )


def _adopt_source_table_skeleton(
    source: Path,
    candidate: Path,
    tables: list[Any],
    *,
    merge_phantom_rows: bool,
) -> tuple[list[Any], dict[int, tuple[list[Any], list[Any]]]]:
    """Swap MinerU's redrawn tables for the source's own, page by page.

    MinerU's render_pdf(ORIGINAL) does not keep a source table's ruled
    grid; it draws its own from its parse -- confirmed directly on a real
    page: the source's 129.5 / 311.6 columns came back as 39 / 403 and its
    rows were squashed to under half their height, so every English label
    in the starved first column broke mid-word at 4.5pt. Translating on that
    grid can only ever patch around it. Wherever the source page itself has
    a ruled table, use the SOURCE table (its geometry and its own cell text)
    instead; the candidate's tables only tell us which MinerU-drawn regions
    to clear, so their row/column structure does not need to match.

    Returns the tables to translate (source tables on adopted pages,
    candidate tables everywhere else) and, per adopted page, the candidate
    regions that must be cleared before the source grid is redrawn. A page
    is not adopted only if some OTHER candidate content sits where the
    source table will go -- redrawing there would overwrite it.
    """
    import fitz

    from . import pdf_table

    try:
        source_tables = pdf_table.extract_pdf_tables(source, merge_phantom_rows=merge_phantom_rows)
    except pdf_table.PdfTableError:
        return tables, {}
    result: list[Any] = []
    adopted: dict[int, tuple[list[Any], list[Any]]] = {}
    candidate_doc = fitz.open(candidate)
    try:
        for page_number in sorted({table.page_number for table in tables}):
            candidate_page_tables = sorted((t for t in tables if t.page_number == page_number), key=lambda t: t.rect[1])
            source_page_tables = sorted((t for t in source_tables if t.page_number == page_number), key=lambda t: t.rect[1])
            if not source_page_tables:
                result.extend(candidate_page_tables)
                continue
            source_rects = [fitz.Rect(t.rect) for t in source_page_tables]
            replaced = [t for t in candidate_page_tables if any(fitz.Rect(t.rect).intersects(r) for r in source_rects)]
            source_ids = {cell.id for t in source_page_tables for cell in t.cells}
            # A MinerU table elsewhere on the page keeps the existing path,
            # unless its cell IDs would collide with the source table's.
            untouched = [
                t for t in candidate_page_tables
                if t not in replaced and not ({cell.id for cell in t.cells} & source_ids)
            ]
            page = candidate_doc[page_number - 1]
            candidate_rects = [fitz.Rect(t.rect) + (-2, -2, 2, 2) for t in replaced]

            def owned(rect: Any) -> bool:
                return any(rect in owner for owner in candidate_rects)

            conflict = False
            for source_table in source_page_tables:
                target = fitz.Rect(source_table.rect)
                for block in page.get_text("dict", clip=target).get("blocks", ()):
                    for line in block.get("lines", ()) if block.get("type") == 0 else ():
                        for span in line.get("spans", ()):
                            box = fitz.Rect(span["bbox"])
                            if span.get("text", "").strip() and box.intersects(target) and not owned(box):
                                conflict = True
                for info in page.get_image_info():
                    box = fitz.Rect(info["bbox"])
                    if box.intersects(target) and not owned(box):
                        conflict = True
            if conflict:
                result.extend(candidate_page_tables)
                continue
            result.extend(source_page_tables)
            result.extend(untouched)
            adopted[page_number] = (candidate_rects + source_rects, source_page_tables)
    finally:
        candidate_doc.close()
    return result, adopted


def _source_cell_alignment(source_page: Any, cell: Any) -> tuple[int, bool]:
    """Return (horizontal align, vertically centred) as the source cell has it."""
    import fitz

    cell_rect = fitz.Rect(cell.rect)
    text_rect = fitz.Rect()
    for block in source_page.get_text("dict", clip=cell_rect).get("blocks", ()):
        for line in block.get("lines", ()) if block.get("type") == 0 else ():
            for span in line.get("spans", ()):
                box = fitz.Rect(span["bbox"])
                if span.get("text", "").strip() and cell_rect.contains(fitz.Point((box.x0 + box.x1) / 2, (box.y0 + box.y1) / 2)):
                    text_rect |= box
    if text_rect.is_empty:
        return 0, False
    centred = (
        abs((text_rect.x0 + text_rect.x1) / 2 - (cell_rect.x0 + cell_rect.x1) / 2) <= max(3.0, cell_rect.width * 0.08)
        and text_rect.width <= cell_rect.width * 0.85
    )
    middle = (
        abs((text_rect.y0 + text_rect.y1) / 2 - (cell_rect.y0 + cell_rect.y1) / 2) <= max(3.0, cell_rect.height * 0.15)
        and text_rect.height <= cell_rect.height * 0.75
    )
    return (1 if centred else 0), middle


def _render_on_source_skeleton(
    source: Path,
    candidate: Path,
    page_number: int,
    page_tables: list[Any],
    cell_translations: dict[str, str],
    clear_rects: list[Any],
    *,
    page_links: dict[int, list[dict]],
) -> dict[str, object] | None:
    """Render one page's tables on the source's own grid, typeset like the source.

    Clears MinerU's redrawn table, then lays the translation out the way
    the source table is laid out: one font size for the whole table, never
    a word split across lines, each cell aligned as its source cell is
    (label column and header centred both ways, body text left/top), within
    the source table's own height. The size rule is: start at the source's
    own size; if the text does not fit the original height, step the size
    down until it does -- never fall back to another layout and never grow
    the table past its original area. The grid is drawn once at its final
    geometry, so nothing has to be shifted or rasterized afterwards.
    Returns the result dict on success; None (only on an unexpected render
    error) leaves the candidate untouched for the caller's existing path.
    """
    import dataclasses

    import fitz

    from . import pdf_table

    source_doc = fitz.open(source)
    work_doc = fitz.open(candidate)
    staged = candidate.with_name(candidate.stem + ".skeleton" + candidate.suffix)
    patched = candidate.with_name(candidate.stem + ".skeleton-patched" + candidate.suffix)
    try:
        source_page = source_doc[page_number - 1]
        page = work_doc[page_number - 1]
        for rect in clear_rects:
            page.add_redact_annot(fitz.Rect(rect) + (-1, -1, 1, 1), fill=None)
        # graphics=1: remove only paths lying wholly inside these regions
        # (MinerU's own grid), never a larger path merely crossing them.
        page.apply_redactions(images=0, graphics=1, text=0)

        cells = [cell for table in page_tables for cell in table.cells]
        detected = {cell.id: _source_cell_alignment(source_page, cell) for cell in cells if cell.rect is not None}
        # Alignment is a property of a column, not of one cell: a short
        # sentence in a left-aligned body column can sit near its cell's
        # centre by coincidence (observed: one such cell came out centred).
        # Body rows take their column's majority; the header row keeps its
        # own, since headers are commonly centred over left-aligned columns.
        alignment: dict[str, tuple[int, bool]] = {}
        for table in page_tables:
            header_row = min((cell.row for cell in table.cells if cell.rect is not None), default=1)
            for column in {cell.column for cell in table.cells}:
                body = [detected[c.id] for c in table.cells if c.column == column and c.row != header_row and c.id in detected]
                if body:
                    majority = (
                        1 if sum(h for h, _ in body) * 2 > len(body) else 0,
                        sum(1 for _, m in body if m) * 2 > len(body),
                    )
                for cell in table.cells:
                    if cell.column != column or cell.id not in detected:
                        continue
                    alignment[cell.id] = detected[cell.id] if cell.row == header_row or not body else majority
        union = fitz.Rect()
        for table in page_tables:
            union |= fitz.Rect(table.rect)
        style = _sample_grid_line_style(source_page, union) or ((0.0, 0.0, 0.0), 0.5)

        fonts: dict[str, fitz.Font] = {}

        def needed_height(cell: Any, size: float) -> float | None:
            """Height this cell's translation needs at ``size``; None if a word would split."""
            text = cell_translations.get(cell.id, "")
            if cell.rect is None or not text.strip():
                return 0.0
            rect = fitz.Rect(cell.rect)
            normalised = pdf_table._normalise_render_text(text)
            fontfile = str(_table_cell_font(cell, normalised))
            font = fonts.setdefault(fontfile, fitz.Font(fontfile=fontfile))
            pad = pdf_table._cell_fit_padding(rect, normalised, 2.0)
            width = rect.width - 2 * pad
            for paragraph in normalised.split("\n"):
                for atom, _ in pdf_table._tokenize_atoms_with_seps(paragraph):
                    # Only a single word wider than the cell is a hard
                    # failure; a too-wide phrase just wraps between words.
                    if any(font.text_length(word, fontsize=size) > width for word in atom.split(" ")):
                        return None
            wrapped = pdf_table._wrap_atomic_phrases(
                normalised, fontfile=fontfile, fontname="probe", fontsize=size, max_width=width
            )
            probe = fitz.open()
            try:
                leftover = probe.new_page(width=width + 20, height=4000).insert_textbox(
                    fitz.Rect(0, 0, width, 4000), wrapped, fontname="probe", fontfile=fontfile, fontsize=size
                )
            finally:
                probe.close()
            if leftover < 0:
                return None
            return (4000 - leftover) + 2 * pad + 1.0


        def layout(table: Any, size: float) -> dict[int, tuple[float, float]] | None:
            """New (top, bottom) per row, rebalancing height within the table.

            Each row gets the height its tallest cell needs at ``size``; if
            those sum to no more than the table's source height, whatever is left
            is shared back out in proportion to the SOURCE row heights, so
            the table still fills its original area in its original
            proportions. Rows the source made generous give up room they
            do not need to rows that are tight, instead of the tightest row
            alone dictating a small font for the whole table.
            """
            rows = sorted({cell.row for cell in table.cells if cell.rect is not None})
            bottoms = {
                row: min(cell.rect[3] for cell in table.cells if cell.row == row and cell.rect is not None)
                for row in rows
            }
            source_heights: dict[int, float] = {}
            previous = table.rect[1]
            for row in rows:
                source_heights[row] = bottoms[row] - previous
                previous = bottoms[row]
            required = {row: 0.0 for row in rows}
            spans: list[tuple[int, int, float]] = []
            for cell in table.cells:
                if cell.rect is None:
                    continue
                need = needed_height(cell, size)
                if need is None:
                    return None
                last = max(row for row in rows if bottoms[row] <= cell.rect[3] + 0.5)
                if last == cell.row:
                    required[cell.row] = max(required[cell.row], need)
                else:
                    spans.append((cell.row, last, need))
            source_total = sum(source_heights.values())
            available = source_total
            total_required = sum(required.values())
            if total_required > available + 1e-6:
                return None
            spare = max(total_required, source_total) - total_required
            heights = {row: required[row] + spare * source_heights[row] / source_total for row in rows}
            for first, last, need in spans:
                if sum(heights[row] for row in rows if first <= row <= last) < need:
                    return None
            result: dict[int, tuple[float, float]] = {}
            top = table.rect[1]
            for row in rows:
                result[row] = (top, top + heights[row])
                top += heights[row]
            return result

        top_size = max((cell.source_font_size or 10.0) for cell in cells if cell.rect is not None)
        size = round(top_size * 2) / 2
        layouts: dict[int, dict[int, tuple[float, float]]] | None = None
        # Step down until it fits; the floor only stops a runaway loop on a
        # pathological table (a word wider than its column even at 1pt).
        while size >= 1.0 - 1e-9:
            attempt = {table.table_number: layout(table, size) for table in page_tables}
            if all(value is not None for value in attempt.values()):
                layouts = attempt  # type: ignore[assignment]
                break
            size = round(size - 0.5, 2)
        if layouts is None:
            return None

        adjusted_tables = []
        shift_ranges: list[tuple[Any, Any]] = []
        grown_rows = 0
        for table in page_tables:
            row_spans = layouts[table.table_number]
            rows = sorted(row_spans)
            bottoms = {
                row: min(cell.rect[3] for cell in table.cells if cell.row == row and cell.rect is not None)
                for row in rows
            }
            new_cells = []
            for cell in table.cells:
                if cell.rect is None:
                    new_cells.append(cell)
                    continue
                last = max(row for row in rows if bottoms[row] <= cell.rect[3] + 0.5)
                x0, _, x1, _ = cell.rect
                new_cells.append(dataclasses.replace(
                    cell, rect=(x0, row_spans[cell.row][0], x1, row_spans[last][1]), source_font_size=size
                ))
            tx0, ty0, tx1, ty1 = table.rect
            new_bottom = row_spans[rows[-1]][1]
            grown_rows += int(new_bottom > ty1 + 0.5)
            adjusted_tables.append(dataclasses.replace(table, rect=(tx0, ty0, tx1, new_bottom), cells=tuple(new_cells)))
            shift_ranges.extend((cell.rect, new.rect) for cell, new in zip(table.cells, new_cells) if cell.rect is not None)

        shape = page.new_shape()
        for table in adjusted_tables:
            for cell in table.cells:
                if cell.rect is not None:
                    shape.draw_rect(fitz.Rect(cell.rect))
        shape.finish(color=style[0], width=style[1], fill=None)
        shape.commit(overlay=True)
        work_doc.save(str(staged))
        work_doc.close()
        work_doc = None

        links = dict(page_links)
        if page_number in links:
            remapped = []
            for link in links[page_number]:
                origin = fitz.Rect(link["from"])
                for old, new in shift_ranges:
                    if abs(fitz.Rect(old).y0 - origin.y0) < 0.5 and abs(fitz.Rect(old).x0 - origin.x0) < 0.5:
                        link = {**link, "from": fitz.Rect(new)}
                        break
                remapped.append(link)
            links[page_number] = remapped

        page_cell_ids = {cell.id for cell in cells}
        report = pdf_table.render_table_translations(
            staged,
            patched,
            {cell_id: text for cell_id, text in cell_translations.items() if cell_id in page_cell_ids},
            tables=adjusted_tables,
            fontfile=_table_cell_font,
            minimum_font_size=size,
            initial_font_size=size,
            align=lambda cell: alignment.get(cell.id, (0, False))[0],
            middle_aligned=lambda cell: alignment.get(cell.id, (0, False))[1],
            page_links=links,
            spread_lines=False,
        )
        patched.replace(candidate)
        return {
            "table_count": report.table_count,
            "cell_count": report.cell_count,
            "rendered_cell_count": report.rendered_cell_count,
            "restored_link_count": report.restored_link_count,
            "font_size": size,
            "tables_taller_than_source": grown_rows,
        }
    except pdf_table.PdfTableError:
        return None
    finally:
        source_doc.close()
        if work_doc is not None:
            work_doc.close()
        staged.unlink(missing_ok=True)
        patched.unlink(missing_ok=True)


def _translate_tables(
    provider: TranslationBatchProvider,
    source: Path,
    candidate: Path,
    *,
    source_hash: str,
    source_language: str,
    target_language: str,
    minimum_font_size: float,
    merge_phantom_rows: bool = True,
    allowed_pages: Iterable[int] | None = None,
    geometry_source: Path | None = None,
) -> dict[str, object]:
    """Patch this candidate's vector tables through the dedicated cell path.

    ``geometry_source`` (the untouched source PDF) enables rendering on the
    source's own table grid wherever it matches; see
    _adopt_source_table_skeleton().

    _make_units_from_current_block() above deliberately skips every
    "table"-typed block: the general render_pdf(ORIGINAL) layout has no
    per-cell wrap/shrink protection, so a translation longer than the
    source could silently overflow or overlap a fixed-width cell (the
    exact failure this project spent real effort chasing down in the
    PyMuPDF-native fallback path). pdf_table.py already exists
    specifically to translate tables safely -- geometry-aware cells, a
    fail-closed fit check -- but nothing in this pipeline ever called it,
    so every table on every page was published completely untranslated.
    Patch the already-rendered candidate, whose table regions still hold
    the original English exactly because render_pdf never touched them,
    so the rest of the page keeps its MinerU-rendered translation.

    merge_phantom_rows=False is for _try_table_translation() below: a
    dense equipment schedule can legitimately have several real,
    independently-bounded rows in ONE column (a second and third pump
    spec for the same lift station) sharing a single rowspanned cell in
    every OTHER column. _merge_phantom_rows() was written for a
    different shape -- one genuine reply artificially split by a
    hyperlink's own underline being misread as a row divider -- and
    cannot tell the two apart; on this shape it folds those distinct
    rows into one multi-line cell, which then renders with the ORIGINAL
    row-divider line still drawn across the middle of the merged text.
    Every other current caller of this function still wants the
    original merge, so this only turns it off where it was actually
    wrong.

    ``allowed_pages`` (source_manifest.table_pages, i.e. what MinerU's own
    native parse -- run on the untouched source, before any redaction --
    already classified as containing a table) guards against a false
    positive this function's own caller can create: _restore_layout()
    (which always runs first) redacts a multi-line article_text/
    centered_text paragraph one source LINE at a time, leaving several
    white redaction rectangles tiled edge-to-edge down the candidate
    page. find_tables(strategy="lines_strict") reads the shared edges
    between those tiles as ruled grid lines and reports the whole
    paragraph as a several-row "table" (observed: two ordinary articles
    on this project's own test document each misdetected this way).
    Re-translating that "table" cell-by-cell then re-redacts and
    re-renders small fragments of the already-correctly-restored
    paragraph, corrupting it. A page MinerU's own source-side parse
    never flagged as tabular cannot have gained a real one from
    translation, so skip it entirely when this filter is given.
    """
    from . import pdf_table

    try:
        tables = pdf_table.extract_pdf_tables(candidate, merge_phantom_rows=merge_phantom_rows)
    except pdf_table.PdfTableError as exc:
        return {"status": "skipped", "reason": str(exc)}
    if allowed_pages is not None:
        allowed = set(allowed_pages)
        tables = [table for table in tables if table.page_number in allowed]
    if not tables:
        return {"status": "skipped", "reason": "no vector tables detected"}
    candidate_tables = tables
    skeleton_regions: dict[int, list[Any]] = {}
    if geometry_source is not None:
        tables, skeleton_regions = _adopt_source_table_skeleton(
            geometry_source, candidate, tables, merge_phantom_rows=merge_phantom_rows
        )

    # A hyperlink is a page annotation, not page content, and MinerU's own
    # render_pdf(ORIGINAL) step -- which runs before this function is ever
    # called -- does not carry annotations over from the source it parsed:
    # the candidate this function receives has already lost every link on
    # the whole page, table or not. Read them from the true, untouched
    # source instead -- but resolve each one to a cell by CONTENT, not by
    # reusing its source rectangle against the candidate's cells: an
    # earlier row's translation running longer or shorter than its
    # source text shifts every row below it, so a link's original
    # position can drift onto a completely different, unrelated cell by
    # the time MinerU is done re-rendering the page. The text the link
    # itself covers in the source is stable regardless of that drift,
    # and matching it against each cell's own (still-untranslated at
    # this point) text finds the right cell directly. A short match
    # ("Clarifications" alone, say) can land on several cells at once;
    # only a long enough match is trusted, and an ambiguous or
    # unmatched link is simply not restored rather than guessed at --
    # attaching it to the wrong cell would be worse than dropping it.
    import fitz

    source_page_links: dict[int, list[dict]] = {}
    source_doc = fitz.open(source)
    try:
        for page_index, source_page in enumerate(source_doc, start=1):
            links = [link for link in source_page.get_links() if link.get("kind") == 2 and link.get("from") is not None]
            if not links:
                continue
            page_cells = [
                (cell, cell.text.replace("\n", " "))
                for table in tables if table.page_number == page_index
                for cell in table.cells if cell.rect is not None and cell.text.strip()
            ]
            for link in links:
                raw_text = source_page.get_text("text", clip=fitz.Rect(link["from"])).strip()
                cleaned = " ".join(part for part in raw_text.split() if len(part) > 1)
                if len(cleaned) < 15:
                    continue
                matches = [cell for cell, text in page_cells if cleaned in text]
                if len(matches) != 1:
                    continue
                source_page_links.setdefault(page_index, []).append(
                    {"kind": link["kind"], "uri": link.get("uri", ""), "from": fitz.Rect(matches[0].rect)}
                )
    finally:
        source_doc.close()

    units: list[TranslationUnit] = []
    for table in tables:
        for cell in table.cells:
            if cell.is_empty:
                continue
            cell_text, dates = localize_chinese_dates(cell.text, source_language, target_language)
            data = {
                "document_hash": source_hash,
                "format": DocumentFormat.PDF,
                "location": DocumentLocation(part=f"page:{cell.page_number}", object_id=cell.id),
                "source_language": source_language,
                "target_language": target_language,
                "source_text": cell_text,
                "protected_tokens": rule_protected_tokens(cell_text, dates),
                "style_signature": "table_cell",
                "context_before": "",
                "context_after": "",
            }
            units.append(TranslationUnit(id=generate_unit_id(**data), **data))
    if not units:
        return {"status": "skipped", "reason": "every table cell is empty", "table_count": len(tables)}

    by_id = {unit.id: unit for unit in units}
    translations: dict[str, str] = {}
    warnings: list[dict[str, object]] = []
    for start in range(0, len(units), 24):
        batch = units[start : start + 24]
        results = _translate_batch_with_retry(provider, batch)
        if len(results) != len(batch):
            raise RuntimeError("translation provider returned an incomplete table-cell batch")
        expected = {unit.id: unit for unit in batch}
        for result in results:
            unit = expected.get(result.unit_id)
            if unit is None:
                raise RuntimeError("translation provider returned an unknown table-cell unit ID")
            errors = validate_result_for_unit(unit, result)
            if result.validation_status == "needs_review" and result.error:
                errors = list(dict.fromkeys([*errors, *result.error.split("; ")]))
            if errors:
                warnings.append({"unit_id": result.unit_id, "cell_id": unit.location.object_id, "errors": errors})
            translations[unit.id] = result.translation
    if warnings:
        translations, warnings = _remediate_warnings(provider, by_id, translations, warnings)

    cell_translations: dict[str, str] = {}
    for table in tables:
        for cell in table.cells:
            if cell.is_empty:
                cell_translations[cell.id] = ""
    for unit in units:
        cell_translations[unit.location.object_id] = translations[unit.id]

    # Each page's own copy of a repeating column header (this project's own
    # multi-page clarification tables repeat "Sr #", "Reference Section",
    # etc. on every page) is sent to the provider as its own independent
    # unit, batched alongside whatever other cells happen to share that
    # request -- nothing ties separate units for byte-identical source text
    # to the same output, so the provider is free to (and observed to)
    # answer "Sr #" as "序号" on one page, "序号" on its own line from "#" on
    # another, and "编号" on a third. Force every cell with EXACTLY the same
    # source text to the same translation -- the first one produced, in
    # document order -- so a repeating header or boilerplate label reads
    # identically everywhere instead of drifting page to page.
    canonical_translation_by_text: dict[str, str] = {}
    for table in tables:
        for cell in table.cells:
            if cell.is_empty:
                continue
            key = cell.text.strip()
            existing = canonical_translation_by_text.get(key)
            if existing is None:
                canonical_translation_by_text[key] = cell_translations[cell.id]
            else:
                cell_translations[cell.id] = existing

    # validate_pdf_table_translations() enforces a stricter per-cell contract
    # than the rest of this pipeline (a table label must keep its English
    # spelling, not just a Chinese rendering) and raises for the WHOLE
    # mapping it is given. Feeding it every table at once meant one label
    # a retry could not fix -- e.g. "Gulshan e Ravi" losing its English
    # spelling -- discarded translation for every table on every page,
    # publishing all of them still in raw English. Validate one table at a
    # time instead, so a table that still fails after remediation is
    # dropped (its own region keeps the untranslated source, per this
    # module's existing fail-closed contract) without taking every other,
    # cleanly-translated table down with it.
    good_tables: list[pdf_table.PdfTable] = []
    for table in tables:
        table_cells = {cell.id: cell_translations[cell.id] for cell in table.cells}
        try:
            pdf_table.validate_pdf_table_translations(
                (table,), table_cells, source_language=source_language, target_language=target_language
            )
        except pdf_table.PdfTableError as exc:
            warnings.append({
                "unit_id": None,
                "table": f"page:{table.page_number}:table:{table.table_number}",
                "errors": [str(exc)],
            })
            continue
        good_tables.append(table)
    if not good_tables and not skeleton_regions:
        return {
            "status": "skipped",
            "reason": "every table failed pre-render validation",
            "table_count": len(tables),
            "cell_count": len(units),
            "warnings": warnings,
        }

    good_cell_ids = {cell.id for table in good_tables for cell in table.cells}
    good_cell_translations = {cell_id: text for cell_id, text in cell_translations.items() if cell_id in good_cell_ids}
    patched = candidate.with_name(candidate.stem + ".tables-patched" + candidate.suffix)

    # Source tables are always laid out on the source's own grid, one page at
    # a time; the font steps down until everything fits (see
    # _render_on_source_skeleton). A table whose translation failed
    # validation is still drawn on that grid -- with its source text, the
    # same fail-closed content this module has always kept -- so the page's
    # layout never depends on whether one table translated cleanly. Only an
    # unexpected rendering error sends a page back to MinerU's grid below.
    skeleton_reports: dict[int, dict[str, object]] = {}
    rendered_source_tables: list[Any] = []
    for page_number, (regions, source_page_tables) in skeleton_regions.items():
        page_translations = dict(good_cell_translations)
        for table in source_page_tables:
            if table not in good_tables:
                page_translations.update({cell.id: cell.text for cell in table.cells})
        page_report = _render_on_source_skeleton(
            geometry_source,
            candidate,
            page_number,
            source_page_tables,
            page_translations,
            regions,
            page_links=source_page_links,
        )
        if page_report is not None:
            skeleton_reports[page_number] = page_report
            rendered_source_tables.extend(source_page_tables)
    if skeleton_regions:
        fallback_pages = set(skeleton_regions) - set(skeleton_reports)
        good_ids_by_page = {
            page: {cell.id for t in good_tables if t.page_number == page for cell in t.cells} for page in fallback_pages
        }
        all_source_tables = [t for _, page_list in skeleton_regions.values() for t in page_list]
        good_tables = [t for t in good_tables if t not in all_source_tables] + [
            t for t in candidate_tables
            if t.page_number in fallback_pages and {cell.id for cell in t.cells} <= good_ids_by_page[t.page_number]
        ]
        tables = [t for t in tables if t not in all_source_tables] + [
            t for t in candidate_tables if t.page_number in fallback_pages and t not in tables
        ]
    if skeleton_reports and not good_tables:
        return _merge_skeleton_reports(None, skeleton_reports, tables, warnings)

    # A translation into a language that runs visually wider than the source
    # (English expanding a Chinese table's own short phrases) can overflow
    # more than one row in the same table, and growing every such row
    # toward a generously large target font competes for the SAME finite
    # strip of page space below the table -- confirmed directly: aiming
    # high for each of several overflowing rows in turn left too little
    # room for the last one, which then failed even at the bare floor,
    # reverting the whole table to its untranslated source. Sweep the
    # growth target from the floor upward instead of committing to one
    # guess: attempt the whole table at the floor first (the one quality
    # level every row can always afford together, so it succeeding is
    # never in doubt if the table can be translated at all), then retry
    # at floor+1, +2, ... against a FRESH copy of this candidate each
    # time, keeping the LAST attempt that still renders every cell and
    # discarding the first one that doesn't.
    original_candidate_bytes = candidate.read_bytes()
    best_result: dict[str, object] | None = None
    best_candidate_bytes: bytes | None = None
    quality = minimum_font_size
    max_quality = minimum_font_size + 6.0
    while quality <= max_quality + 1e-9:
        result, _ = _attempt_table_render(
            candidate,
            patched,
            original_candidate_bytes,
            tables,
            good_tables,
            good_cell_translations,
            minimum_font_size=minimum_font_size,
            growth_target_size=quality,
            source_page_links=source_page_links,
            units=units,
            warnings=warnings,
        )
        if result["status"] in ("patched", "partially_patched"):
            best_result = result
            best_candidate_bytes = candidate.read_bytes()
            quality += 1.0
            continue
        if best_result is None:
            best_result = result
        break
    candidate.write_bytes(best_candidate_bytes if best_candidate_bytes is not None else original_candidate_bytes)
    if skeleton_reports:
        return _merge_skeleton_reports(best_result, skeleton_reports, tables, warnings)
    return best_result


def _merge_skeleton_reports(
    legacy: dict[str, object] | None,
    skeleton_reports: dict[int, dict[str, object]],
    tables: list[Any],
    warnings: list[dict[str, object]],
) -> dict[str, object]:
    """Combine source-grid page results with the MinerU-grid path's result."""
    skeleton_tables = sum(int(r["table_count"]) for r in skeleton_reports.values())
    merged: dict[str, object] = {
        "table_count": skeleton_tables,
        "cell_count": sum(int(r["cell_count"]) for r in skeleton_reports.values()),
        "rendered_cell_count": sum(int(r["rendered_cell_count"]) for r in skeleton_reports.values()),
        "restored_link_count": sum(int(r["restored_link_count"]) for r in skeleton_reports.values()),
        "patched_pages": sorted(skeleton_reports),
        "source_grid_pages": {
            page: {"font_size": r["font_size"], "tables_taller_than_source": r["tables_taller_than_source"]}
            for page, r in skeleton_reports.items()
        },
        "warnings": warnings,
    }
    legacy_ok = legacy is None or legacy.get("status") in ("patched", "partially_patched")
    if legacy is not None and legacy_ok:
        for key in ("table_count", "cell_count", "rendered_cell_count", "restored_link_count"):
            merged[key] = int(merged[key]) + int(legacy.get(key, 0) or 0)
        merged["patched_pages"] = sorted(set(merged["patched_pages"]) | set(legacy.get("patched_pages", ())))
    all_done = legacy is None or legacy.get("status") == "patched"
    merged["status"] = "patched" if all_done and int(merged["table_count"]) >= len(tables) else "partially_patched"
    if legacy is not None and not legacy_ok:
        merged["legacy_reason"] = legacy.get("reason")
    return merged


def _middle_json(result: object) -> dict[str, Any]:
    middle_json = getattr(result, "middle_json", None)
    if isinstance(middle_json, dict):
        return middle_json
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, dict):
            return payload
    raise RuntimeError("MinerU parse result does not expose a mutable middle_json document")


def _extract_text_units(
    payload: dict[str, Any],
    *,
    source_hash: str,
    source_language: str,
    target_language: str,
) -> list[_TextUnit]:
    units: list[_TextUnit] = []
    if isinstance(payload.get("pdf_info"), list):
        for page_number, page_info in enumerate(payload["pdf_info"], 1):
            if not isinstance(page_info, dict):
                continue
            for block_index, block in enumerate(page_info.get("para_blocks", [])):
                for child_index, leaf in enumerate(_legacy_text_leaves(block)):
                    units.extend(
                        _make_units_from_targets(
                            leaf,
                            page_number=page_number,
                            block_index=f"{block_index}:{child_index}",
                            source_hash=source_hash,
                            source_language=source_language,
                            target_language=target_language,
                        )
                    )
        return units

    pages = payload.get("pages")
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, dict):
                continue
            page_number = int(page.get("page_idx", page.get("page", 0))) + (1 if "page_idx" in page else 0)
            for block_index, block in enumerate(page.get("blocks", [])):
                if isinstance(block, dict):
                    units.extend(
                        _make_units_from_current_block(
                            block,
                            page_number=page_number,
                            block_index=str(block_index),
                            source_hash=source_hash,
                            source_language=source_language,
                            target_language=target_language,
                        )
                    )
    # A page whose entire content MinerU's own block model classifies as
    # "table" or another non-"text" type (observed: a single-page,
    # drawing-heavy clarification page with a dense schedule table and no
    # plain paragraph anywhere) legitimately produces zero units here --
    # this function only ever walks "text"-type leaves. That used to abort
    # the whole document before _translate_tables() or the vector-page
    # native fallback below it ever got a chance to run, even though both
    # exist specifically to translate content this function does not
    # collect (they re-extract directly from the rendered PDF, independent
    # of MinerU's own block classification). Returning empty here instead
    # lets the document continue on to render with the page's own English
    # left in place from render_pdf(ORIGINAL) -- exactly the state the
    # table/vector fallbacks below expect to receive and translate in
    # place, rather than raising before they ever run. A document that is
    # ALSO empty after every one of those fallbacks still surfaces as a
    # visibly untranslated candidate under validate_candidate(), so nothing
    # here can silently publish English as if it were done.
    return units


def _legacy_text_leaves(block: object) -> Iterable[dict[str, Any]]:
    if not isinstance(block, dict):
        return ()
    targets = _span_targets(block)
    if targets:
        return (block,)
    children = block.get("blocks")
    if not isinstance(children, list):
        return ()
    leaves: list[dict[str, Any]] = []
    for child in children:
        leaves.extend(_legacy_text_leaves(child))
    return tuple(leaves)


def _make_units_from_targets(
    block: dict[str, Any],
    *,
    page_number: int,
    block_index: str,
    source_hash: str,
    source_language: str,
    target_language: str,
) -> list[_TextUnit]:
    targets = _span_targets(block)
    text = "".join(str(target[0].get(target[1], "")) for target in targets).strip()
    if not text or not _looks_translatable(text):
        return []
    return [_make_unit(text, targets, page_number, block_index, block.get("type", "text"), block.get("bbox"), source_hash, source_language, target_language)]


def _make_units_from_current_block(
    block: dict[str, Any],
    *,
    page_number: int,
    block_index: str,
    source_hash: str,
    source_language: str,
    target_language: str,
) -> list[_TextUnit]:
    block_type = str(block.get("type", "text")).casefold()
    if block_type == "table":
        # A "table"-typed block isn't just the grid: MinerU nests a
        # table_caption (and sometimes a table_footnote) alongside the
        # table_body as siblings in its own content list. The table_body
        # is deliberately skipped here -- pdf_table.py's dedicated,
        # geometry-aware cell path translates the grid safely -- but that
        # path only ever looks at the rendered grid's own cells, never at
        # a caption sitting above or a footnote below it, and the
        # blanket skip below used to drop the caption on the floor
        # entirely: confirmed directly, a real document's own "表 1 ..."
        # table title published completely untranslated while every cell
        # inside the table itself was fine. Caption/footnote text is
        # ordinary single-line prose, not a grid cell, so it goes through
        # the normal per-unit translation path like any other paragraph.
        units: list[_TextUnit] = []
        for sub_index, sub_block in enumerate(block.get("content") or []):
            if not isinstance(sub_block, dict):
                continue
            sub_type = str(sub_block.get("type", "")).casefold()
            if sub_type not in {"table_caption", "table_footnote"}:
                continue
            units.extend(
                _make_units_from_current_block(
                    sub_block,
                    page_number=page_number,
                    block_index=f"{block_index}:{sub_type}:{sub_index}",
                    source_hash=source_hash,
                    source_language=source_language,
                    target_language=target_language,
                )
            )
        return units
    if block_type in {"image", "chart", "equation", "formula"}:
        return []
    targets: list[tuple[dict[str, Any], str]] = []
    content = block.get("content")
    content_list: list[dict[str, Any]] | None = None
    if isinstance(content, str):
        targets.append((block, "content"))
    elif isinstance(content, list):
        content_list = content
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("content"), str):
                targets.append((item, "content"))
    text = "".join(str(target[0][target[1]]) for target in targets).strip()
    if not text or not _looks_translatable(text):
        return []
    return [_make_unit(text, tuple(targets), page_number, block_index, block_type, block.get("bbox"), source_hash, source_language, target_language, content_list=content_list)]


def _make_unit(
    text: str,
    targets: tuple[tuple[dict[str, Any], str], ...] | list[tuple[dict[str, Any], str]],
    page_number: int,
    block_index: str,
    block_type: object,
    raw_bbox: object,
    source_hash: str,
    source_language: str,
    target_language: str,
    *,
    content_list: list[dict[str, Any]] | None = None,
) -> _TextUnit:
    bbox = _bbox(raw_bbox)
    location = DocumentLocation(part=f"page:{page_number}", object_id=f"mineru:{block_index}")
    text, dates = localize_chinese_dates(text, source_language, target_language)
    unit_data = {
        "document_hash": source_hash,
        "format": DocumentFormat.PDF,
        "location": location,
        "source_language": source_language,
        "target_language": target_language,
        "source_text": text,
        "protected_tokens": rule_protected_tokens(text, dates),
        "style_signature": str(block_type),
        "context_before": "",
        "context_after": "",
    }
    unit = TranslationUnit(id=generate_unit_id(**unit_data), **unit_data)
    return _TextUnit(unit=unit, targets=tuple(targets), page=page_number, block_type=str(block_type), bbox=bbox, content_list=content_list)


def _translate_units(
    provider: TranslationBatchProvider, units: list[_TextUnit]
) -> tuple[dict[str, str], list[dict[str, object]]]:
    """Translate every unit for the whole document before any acceptance check.

    Acceptance happens only after translation finishes: a unit whose content
    still fails validation (residual English, an untranslated date, a
    missing proper name, ...) does not abort the batch or the file. Its
    best-effort translation is kept and the defect is recorded as a warning
    in the returned list, so the rest of the document is still produced.
    Only a structural failure (the provider losing or duplicating a unit)
    still raises, because that would corrupt the rendered PDF.
    """
    translations: dict[str, str] = {}
    warnings: list[dict[str, object]] = []
    by_id = {item.unit.id: item for item in units}
    for start in range(0, len(units), 24):
        batch = units[start : start + 24]
        results = _translate_batch_with_retry(provider, [item.unit for item in batch])
        if len(results) != len(batch):
            raise RuntimeError("translation provider returned an incomplete MinerU batch")
        expected = {item.unit.id: item.unit for item in batch}
        for result in results:
            unit = expected.get(result.unit_id)
            if unit is None:
                raise RuntimeError("translation provider returned an unknown MinerU unit ID")
            errors = validate_result_for_unit(unit, result)
            if result.validation_status == "needs_review" and result.error:
                errors = list(dict.fromkeys([*errors, *result.error.split("; ")]))
            if errors:
                item = by_id.get(result.unit_id)
                warnings.append(
                    {
                        "unit_id": result.unit_id,
                        "page": item.page if item is not None else None,
                        "block_type": item.block_type if item is not None else None,
                        "errors": errors,
                    }
                )
            translations[result.unit_id] = result.translation
    if set(translations) != {item.unit.id for item in units}:
        raise RuntimeError("translation provider did not return every MinerU unit")
    return translations, warnings


def _remediate_warnings(
    provider: TranslationBatchProvider,
    by_id: dict[str, TranslationUnit],
    translations: dict[str, str],
    warnings: list[dict[str, object]],
) -> tuple[dict[str, str], list[dict[str, object]]]:
    """Give the same cloud model one more pass at whatever it still flagged.

    Acceptance happens only after the whole batch has a translation for
    every unit: this runs once, after the initial translate pass returns,
    and only touches the units that were still flagged. A unit that comes
    back clean is adopted and dropped from the warning list; one that is
    still flagged keeps its (possibly improved) translation and stays in
    the report. Shared by both the prose-unit and table-cell translation
    paths, which previously only wired this second pass in for prose --
    a table cell that lost a protected identifier (a drawing/grid
    reference such as ``J01-L3C``) got one internal correction retry
    inside the provider and then nothing further, unlike ordinary text.
    """
    if not warnings:
        return translations, warnings
    flagged_units = [by_id[entry["unit_id"]] for entry in warnings if entry["unit_id"] in by_id]
    if not flagged_units:
        return translations, warnings
    results = {result.unit_id: result for result in _translate_batch_with_retry(provider, flagged_units)}
    remaining: list[dict[str, object]] = []
    for entry in warnings:
        unit_id = entry["unit_id"]
        unit = by_id.get(unit_id)
        result = results.get(unit_id)
        if unit is None or result is None:
            remaining.append(entry)
            continue
        translations[unit_id] = result.translation
        errors = validate_result_for_unit(unit, result)
        if result.validation_status == "needs_review" and result.error:
            errors = list(dict.fromkeys([*errors, *result.error.split("; ")]))
        if errors:
            remaining.append({**entry, "errors": errors})
    return translations, remaining


def _apply_translation(item: _TextUnit, translation: str) -> None:
    if not item.targets:
        return
    if not translation.strip() and item.unit.source_text.strip():
        # A provider occasionally still returns blank text here even after
        # the EMPTY_TRANSLATION remediation retry in _translate_units() /
        # _remediate_warnings() -- validate_result_for_unit() flags it and
        # gives the model one more attempt, but nothing guarantees that
        # retry itself comes back non-blank. Writing an empty string into
        # this block's content crashes the WHOLE document at final
        # MiddleJson serialization (that schema requires non-empty text),
        # so as the last line of defense this one unit is left in its
        # original source language rather than ever doing that -- the
        # existing fail-closed-per-unit policy already used for a table
        # cell that never validates cleanly, applied here too.
        translation = item.unit.source_text
    first, first_key = item.targets[0]
    first[first_key] = translation
    stale = [target for target, _ in item.targets[1:]]
    for target, key in item.targets[1:]:
        target[key] = ""
    if item.content_list is not None and stale:
        stale_ids = {id(entry) for entry in stale}
        item.content_list[:] = [entry for entry in item.content_list if id(entry) not in stale_ids]


def _span_targets(block: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    targets: list[tuple[dict[str, Any], str]] = []
    lines = block.get("lines")
    if not isinstance(lines, list):
        return targets
    for line in lines:
        if not isinstance(line, dict):
            continue
        for span in line.get("spans", []):
            if not isinstance(span, dict):
                continue
            key = "content" if isinstance(span.get("content"), str) else "text" if isinstance(span.get("text"), str) else None
            span_type = str(span.get("type", "text")).casefold()
            if key and span.get(key, "").strip() and span_type in {"text", "inline_text", "contenttype.text"}:
                targets.append((span, key))
    return targets


def _looks_translatable(text: str) -> bool:
    return any(char.isalpha() for char in text) or any("\u4e00" <= char <= "\u9fff" for char in text)


def _bbox(value: object) -> tuple[float, float, float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            return tuple(float(item) for item in value)  # type: ignore[return-value]
        except (TypeError, ValueError):
            return None
    return None


def _write_failed_report(path: Path, *, preflight: PdfPreflight, provider: str, model: str, run: dict[str, object], error: Exception) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": "FAILED",
                "source_hash": preflight.source_hash,
                "classification": preflight.classification,
                "provider": provider,
                "model": model,
                "run": run,
                "error": f"{type(error).__name__}: {error}",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
