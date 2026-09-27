"""MinerU 4 PDF translation service.

MinerU owns PDF parsing and ORIGINAL-layout rendering. PyMuPDF remains the
geometry/audit authority, while the existing translation providers own
bounded semantic batches and protected-token validation.
"""

from __future__ import annotations

import json
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
from document_translator.translation_rules import rule_protected_tokens


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
                        self.provider, units, translations, translation_warnings
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


def _restore_missing_images(source: Path, candidate: Path, middle_json: dict[str, Any]) -> int:
    """Copy each image region straight from the untouched source page.

    MinerU's crop-and-attach step does not reliably populate an image
    payload for every image-type block here (observed: an empty image_body
    with no image_base64/image_path/image_url at all), even though the
    block's bbox is correct. Only the text needs reconstructing, so
    rasterize the same region from the source page directly instead of
    depending on that extraction, and stamp it into the candidate at the
    identical position.
    """
    import fitz

    pages = middle_json.get("pages")
    if not isinstance(pages, list):
        return 0
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
                pixmap = source_page.get_pixmap(clip=rect, dpi=200)
                candidate_page.insert_image(rect, pixmap=pixmap)
                restored += 1
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


def _translate_tables(
    provider: TranslationBatchProvider,
    source: Path,
    candidate: Path,
    *,
    source_hash: str,
    source_language: str,
    target_language: str,
    minimum_font_size: float,
) -> dict[str, object]:
    """Patch this candidate's vector tables through the dedicated cell path.

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
    """
    from . import pdf_table

    try:
        tables = pdf_table.extract_pdf_tables(candidate)
    except pdf_table.PdfTableError as exc:
        return {"status": "skipped", "reason": str(exc)}
    if not tables:
        return {"status": "skipped", "reason": "no vector tables detected"}

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
            data = {
                "document_hash": source_hash,
                "format": DocumentFormat.PDF,
                "location": DocumentLocation(part=f"page:{cell.page_number}", object_id=cell.id),
                "source_language": source_language,
                "target_language": target_language,
                "source_text": cell.text,
                "protected_tokens": rule_protected_tokens(cell.text),
                "style_signature": "table_cell",
                "context_before": "",
                "context_after": "",
            }
            units.append(TranslationUnit(id=generate_unit_id(**data), **data))
    if not units:
        return {"status": "skipped", "reason": "every table cell is empty", "table_count": len(tables)}

    translations: dict[str, str] = {}
    warnings: list[dict[str, object]] = []
    for start in range(0, len(units), 24):
        batch = units[start : start + 24]
        results = provider.translate_batch(batch)
        if len(results) != len(batch):
            raise RuntimeError("translation provider returned an incomplete table-cell batch")
        expected = {unit.id: unit for unit in batch}
        for result in results:
            unit = expected.get(result.unit_id)
            if unit is None:
                raise RuntimeError("translation provider returned an unknown table-cell unit ID")
            errors = validate_result_for_unit(unit, result)
            if errors:
                warnings.append({"unit_id": result.unit_id, "cell_id": unit.location.object_id, "errors": errors})
            translations[unit.id] = result.translation

    cell_translations: dict[str, str] = {}
    for table in tables:
        for cell in table.cells:
            if cell.is_empty:
                cell_translations[cell.id] = ""
    for unit in units:
        cell_translations[unit.location.object_id] = translations[unit.id]

    patched = candidate.with_name(candidate.stem + ".tables-patched" + candidate.suffix)
    try:
        report = pdf_table.render_table_translations(
            candidate,
            patched,
            cell_translations,
            tables=tables,
            fontfile=_table_cell_font,
            minimum_font_size=minimum_font_size,
            page_links=source_page_links,
        )
    except pdf_table.PdfTableError as exc:
        # fail closed, per this module's own contract: a cell that cannot
        # be rendered safely must not silently keep the untranslated
        # English rather than corrupt or overflow the table, but the rest
        # of the page (already rendered by MinerU) is still worth
        # publishing, so this is recorded rather than raised.
        return {"status": "failed", "reason": str(exc), "table_count": len(tables), "cell_count": len(units), "warnings": warnings}
    patched.replace(candidate)
    return {
        "status": "patched",
        "table_count": report.table_count,
        "cell_count": report.cell_count,
        "rendered_cell_count": report.rendered_cell_count,
        "restored_link_count": report.restored_link_count,
        "warnings": warnings,
    }


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
    if not units:
        raise RuntimeError("MinerU result contains no translatable text blocks")
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
    if block_type in {"image", "chart", "table", "equation", "formula"}:
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
    unit_data = {
        "document_hash": source_hash,
        "format": DocumentFormat.PDF,
        "location": location,
        "source_language": source_language,
        "target_language": target_language,
        "source_text": text,
        "protected_tokens": rule_protected_tokens(text),
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
        results = provider.translate_batch([item.unit for item in batch])
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
    units: list[_TextUnit],
    translations: dict[str, str],
    warnings: list[dict[str, object]],
) -> tuple[dict[str, str], list[dict[str, object]]]:
    """Give the same cloud model one more pass at whatever it still flagged.

    Acceptance happens only after the whole document has a translation for
    every unit: this runs once, after `_translate_units` returns, and only
    touches the units that were still flagged. A unit that comes back clean
    is adopted and dropped from the warning list; one that is still flagged
    keeps its (possibly improved) translation and stays in the report.
    """
    if not warnings:
        return translations, warnings
    by_id = {item.unit.id: item for item in units}
    flagged_units = [by_id[entry["unit_id"]].unit for entry in warnings if entry["unit_id"] in by_id]
    if not flagged_units:
        return translations, warnings
    results = {result.unit_id: result for result in provider.translate_batch(flagged_units)}
    remaining: list[dict[str, object]] = []
    for entry in warnings:
        unit_id = entry["unit_id"]
        item = by_id.get(unit_id)
        result = results.get(unit_id)
        if item is None or result is None:
            remaining.append(entry)
            continue
        translations[unit_id] = result.translation
        errors = validate_result_for_unit(item.unit, result)
        if result.validation_status == "needs_review" and result.error:
            errors = list(dict.fromkeys([*errors, *result.error.split("; ")]))
        if errors:
            remaining.append({**entry, "errors": errors})
    return translations, remaining


def _apply_translation(item: _TextUnit, translation: str) -> None:
    if not item.targets:
        return
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
