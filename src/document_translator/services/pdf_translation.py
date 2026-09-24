"""Deprecated legacy PDF fallback.

The formal PDF pipeline is BabelDOC (see :mod:`babeldoc_pdf`).  This module is
kept only so historical diagnostics and tests can still be read; the CLI no
longer imports or invokes it.
"""
from __future__ import annotations
from pathlib import Path
import re
from dataclasses import dataclass
from document_translator.adapters.pdf import read_pdf
from document_translator.core import TranslationResult, TranslationUnit, validate_result_for_unit
from document_translator.font_policy import CJK_FONT, LATIN_FONT, LATIN_NARROW_FONT, contains_cjk

@dataclass(frozen=True, slots=True)
class PdfTranslationOutcome:
    units: tuple[TranslationUnit, ...]
    results: tuple[TranslationResult, ...]

class PdfTranslationService:
    def __init__(self, provider, *, minimum_font_size: float = 6.0):
        self.provider=provider; self.minimum_font_size=minimum_font_size

    def translate_file(self, source_path, destination_path, *, source_language, target_language):
        source,dest=Path(source_path),Path(destination_path)
        if source.resolve()==dest.resolve(): raise ValueError("source and destination paths must differ")
        units=read_pdf(source,source_language=source_language,target_language=target_language)
        results=tuple(self.provider.translate_batch(list(units)) if callable(getattr(self.provider,'translate_batch',None)) else [self.provider.translate_unit(u) for u in units])
        if len(results)!=len(units): raise ValueError("PDF translation count mismatch")
        for u,r in zip(units,results,strict=True):
            errors=validate_result_for_unit(u,r)
            if errors: raise ValueError(f"invalid PDF translation for {u.id}: {', '.join(errors)}")
            if "..." in r.translation or "…" in r.translation:
                raise ValueError(f"truncated PDF translation for {u.id}: ellipsis is not allowed")
        self._rewrite(source,dest,units,results,target_language)
        return PdfTranslationOutcome(units=units,results=results)

    def _rewrite(self, source, dest, units, results, target_language):
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError("PDF support requires PyMuPDF; install the PDF runtime first") from exc
        doc=fitz.open(source); source_image_count = sum(len(page.get_images(full=True)) for page in doc)
        by_block = {}
        for unit, result in zip(units, results, strict=True):
            ids = unit.location.node_ids or [unit.location.object_id or '']
            for pos, node_id in enumerate(ids):
                if node_id.startswith('block:'):
                    by_block[(unit.location.part, node_id)] = (result, pos == 0, unit)
        try:
            for page_no,page in enumerate(doc):
                page_blocks = page.get_text('blocks')
                source_dict = page.get_text('dict')
                for block_no,block in enumerate(page_blocks):
                    key=(f'page:{page_no+1}',f'block:{block_no}'); item=by_block.get(key)
                    if item is None or not item[1]: continue
                    result, _, unit = item
                    rect=fitz.Rect(block[:4]); text=result.translation
                    if text.lstrip().startswith(("Chapter ", "Article ")):
                        text = " ".join(part.strip() for part in text.splitlines() if part.strip())
                    if unit.location.node_ids:
                        members = [fitz.Rect(page_blocks[int(n.split(':')[1])][:4]) for n in unit.location.node_ids if n.startswith('block:')]
                        if members:
                            rect = fitz.Rect(min(r.x0 for r in members), min(r.y0 for r in members), max(r.x1 for r in members), max(r.y1 for r in members))
                    cell_rect = self._table_cell_rect(page, rect)
                    # True redaction removes the original text operators from
                    # the content stream.  Redact individual glyph-span
                    # bounds, not the whole block: table blocks often include
                    # border geometry in their bounding box.
                    span_rects = []
                    for source_block in source_dict['blocks']:
                        if source_block.get('type') != 0:
                            continue
                        for line in source_block.get('lines', []):
                            for span in line.get('spans', []):
                                span_box = fitz.Rect(span.get('bbox', ()))
                                if span.get('text', '').strip() and span_box.intersects(rect):
                                    span_rects.append(span_box + (-0.4, -0.4, 0.4, 0.4))
                    for span_box in span_rects:
                        page.add_redact_annot(span_box, fill=(1, 1, 1))
                    if not span_rects:
                        page.add_redact_annot(rect, fill=(1, 1, 1))
                    page.apply_redactions()
                    if contains_cjk(text):
                        fontfile = r'C:\Windows\Fonts\simhei.ttf'
                    elif len(text) >= 80 and Path(r'C:\Windows\Fonts\ARIALN.TTF').exists():
                        # Match the shared font policy: use Arial Narrow for
                        # genuinely long English paragraphs before reducing
                        # the readable point size.
                        fontfile = r'C:\Windows\Fonts\ARIALN.TTF'
                    else:
                        fontfile = r'C:\Windows\Fonts\arial.ttf'
                    size=10.0
                    spans=source_dict['blocks']
                    for b in spans:
                        if b.get('type')==0:
                            for line in b.get('lines',[]):
                                for span in line.get('spans',[]):
                                    if span.get('text','').strip() and fitz.Rect(span['bbox']).intersects(rect): size=float(span.get('size') or size); break
                    fit_rect = cell_rect or rect
                    if cell_rect is not None:
                        fit_rect = fitz.Rect(fit_rect.x0 + 2, fit_rect.y0 + 2, fit_rect.x1 - 2, fit_rect.y1 - 2)
                    align = self._infer_alignment(page, rect, cell_rect, source_dict)
                    # Headings are semantic units, not fixed-width body
                    # lines. Give them the unused page width so an English
                    # heading is not split merely because the Chinese source
                    # heading occupied a narrow text box.
                    heading = text.lstrip().startswith(("Chapter ", "Article "))
                    if cell_rect is None and rect.width < page.rect.width * 0.85 and 40 < rect.y0 < page.rect.height * 0.8:
                        next_y = min((float(b[1]) for b in page_blocks[block_no + 1:] if float(b[1]) > rect.y1), default=page.rect.height - 18)
                        # Non-table PDF text blocks are often line fragments
                        # with an artificially narrow right edge. Expand to
                        # the page's actual single-column content width for
                        # every semantic object, not only named headings.
                        fit_rect = fitz.Rect(min(rect.x0, 76), rect.y0, page.rect.width - 76, max(rect.y1, next_y - 2))
                    while size>=self.minimum_font_size:
                        rc=page.insert_textbox(fit_rect,text,fontfile=fontfile,fontsize=size,fontname='F0',color=(0,0,0),align=align,overlay=True)
                        if rc>=0: break
                        page.draw_rect(fit_rect,color=None,fill=(1,1,1),overlay=True)
                        # Use genuine free space below this source block
                        # before reducing readability.  Never cross the next
                        # block on the same horizontal band.
                        if fit_rect == (cell_rect or rect):
                            below = [float(b[1]) for b in page_blocks[block_no + 1:] if float(b[1]) > rect.y1 and float(b[0]) < rect.x1 and float(b[2]) > rect.x0]
                            candidate = (min(below) - 1.0) if below else (page.rect.height - 18.0)
                            if candidate > rect.y1 + 2.0:
                                fit_rect = fitz.Rect(rect.x0, rect.y0, rect.x1, candidate)
                                continue
                        size=round(size-0.5,1)
                    if size<self.minimum_font_size: raise ValueError(f"PDF block overflow on page {page_no+1}, block {block_no}: {text[:80]}")
            doc.save(dest)
        finally: doc.close()
        # Re-open the produced file and fail closed on untranslated source
        # glyphs or geometry that escaped the page.  This is especially
        # important for table cells: PDF engines may split a cell into
        # several drawing fragments, so a successful API response alone is
        # not proof that every fragment was replaced.
        self._validate_output(dest, target_language, expected_image_count=source_image_count)

    @staticmethod
    def _table_cell_rect(page, text_rect):
        """Return the complete grid cell containing a text block, if any."""
        import fitz
        horizontal = sorted({round(float(d['rect'].y0), 2) for d in page.get_drawings()
                             if abs(float(d['rect'].y1) - float(d['rect'].y0)) < 0.5 and float(d['rect'].width) > 80})
        vertical = sorted({round(float(d['rect'].x0), 2) for d in page.get_drawings()
                           if abs(float(d['rect'].x1) - float(d['rect'].x0)) < 0.5 and float(d['rect'].height) > 80})
        if len(horizontal) < 2 or len(vertical) < 2:
            return None
        cx = (text_rect.x0 + text_rect.x1) / 2
        cy = (text_rect.y0 + text_rect.y1) / 2
        for y0, y1 in zip(horizontal, horizontal[1:]):
            for x0, x1 in zip(vertical, vertical[1:]):
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    return fitz.Rect(x0, y0, x1, y1)
        return None

    @staticmethod
    def _infer_alignment(page, source_rect, cell_rect, source_dict):
        """Infer left/center/right alignment from the original glyph bounds."""
        import fitz
        lines = []
        for block in source_dict.get('blocks', []):
            if block.get('type') != 0:
                continue
            for line in block.get('lines', []):
                line_boxes = []
                for span in line.get('spans', []):
                    box = fitz.Rect(span.get('bbox', ()))
                    if span.get('text', '').strip() and box.intersects(source_rect):
                        line_boxes.append(box)
                if line_boxes:
                    lines.append((min(b.x0 for b in line_boxes), max(b.x1 for b in line_boxes)))
        if not lines:
            return 0
        container = cell_rect or page.rect
        width = max(container.width, 1)
        centers = [((left + right) / 2) for left, right in lines]
        center = (container.x0 + container.x1) / 2
        if sum(abs(value - center) <= width * 0.08 for value in centers) >= max(1, int(len(centers) * 0.8)):
            return 1
        if sum((container.x1 - right) <= width * 0.12 and (left - container.x0) >= width * 0.25 for left, right in lines) >= max(1, int(len(lines) * 0.8)):
            return 2
        return 0

    @staticmethod
    def _validate_output(path: Path, target_language: str, *, expected_image_count: int | None = None) -> None:
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError("PDF support requires PyMuPDF; install the PDF runtime first") from exc
        check_cjk = target_language.lower().startswith(("en", "fr", "de", "es", "it"))
        doc = fitz.open(path)
        try:
            failures: list[str] = []
            if expected_image_count is not None:
                actual_images = sum(len(page.get_images(full=True)) for page in doc)
                if actual_images < expected_image_count:
                    failures.append(f"images lost: expected at least {expected_image_count}, got {actual_images}")
            for page_no, page in enumerate(doc, 1):
                page_rect = page.rect
                for block in page.get_text("blocks"):
                    rect = fitz.Rect(block[:4])
                    if rect.x0 < -0.5 or rect.y0 < -0.5 or rect.x1 > page_rect.width + 0.5 or rect.y1 > page_rect.height + 0.5:
                        failures.append(f"page {page_no}: text outside page")
                    if check_cjk and any("\u4e00" <= ch <= "\u9fff" for ch in str(block[4])):
                        failures.append(f"page {page_no}: untranslated CJK text")
            if failures:
                raise ValueError("PDF output validation failed: " + "; ".join(dict.fromkeys(failures)))
        finally:
            doc.close()
