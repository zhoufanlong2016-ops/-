"""Deprecated native PyMuPDF PDF fallback.

The formal PDF pipeline is MinerU 4 (see :mod:`mineru_pdf`). This module is
kept only for historical callers; the CLI no longer imports or invokes it.
"""
from __future__ import annotations
from pathlib import Path
import re
import math
from dataclasses import dataclass
from document_translator.adapters.pdf import read_pdf
from document_translator.core import TranslationResult, TranslationUnit, validate_result_for_unit
from document_translator.font_policy import CJK_FONT, LATIN_FONT, LATIN_NARROW_FONT, contains_cjk

@dataclass(frozen=True, slots=True)
class PdfTranslationOutcome:
    units: tuple[TranslationUnit, ...]
    results: tuple[TranslationResult, ...]
    warnings: tuple[str, ...] = ()

class PdfTranslationService:
    def __init__(self, provider):
        self.provider=provider

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
        overflow_warnings=self._rewrite(source,dest,units,results,target_language)
        return PdfTranslationOutcome(units=units,results=results,warnings=tuple(overflow_warnings))

    def _rewrite(self, source, dest, units, results, target_language):
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError("PDF support requires PyMuPDF; install the PDF runtime first") from exc
        from .pdf_layout import _font_file
        doc=fitz.open(source); source_image_count = sum(len(page.get_images(full=True)) for page in doc)
        overflow_warnings: list[str] = []
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
                # Pass 1: work out every block's redaction rects and render
                # plan without touching the page yet, then apply every
                # redaction in one call.  Calling apply_redactions per block
                # on a page with many blocks and large embedded images
                # causes the content stream (and file size) to blow up.
                plans = []
                page_redact_rects = []
                claimed_cells: list = []
                for block_no,block in enumerate(page_blocks):
                    key=(f'page:{page_no+1}',f'block:{block_no}'); item=by_block.get(key)
                    if item is None or not item[1]: continue
                    result, _, unit = item
                    rect=fitz.Rect(block[:4]); text=result.translation
                    if text.strip() == unit.source_text.strip():
                        # The provider (correctly, per this project's rule
                        # that road/place names and other identifiers stay
                        # in English) returned the source text unchanged.
                        # Redacting and reinserting it anyway would swap the
                        # PDF's own original glyph run for a different font
                        # file and re-fitted geometry for zero benefit --
                        # every untouched block, including coordinate labels
                        # that must render pixel-for-pixel as before, is
                        # left completely alone.
                        continue
                    if text.lstrip().startswith(("Chapter ", "Article ")):
                        text = " ".join(part.strip() for part in text.splitlines() if part.strip())
                    if unit.location.node_ids:
                        members = [fitz.Rect(page_blocks[int(n.split(':')[1])][:4]) for n in unit.location.node_ids if n.startswith('block:')]
                        if members:
                            rect = fitz.Rect(min(r.x0 for r in members), min(r.y0 for r in members), max(r.x1 for r in members), max(r.y1 for r in members))
                    cell_rect = self._table_cell_rect(page, rect)
                    if cell_rect is not None:
                        # PyMuPDF's own get_text('blocks') occasionally
                        # mis-segments a table row, fusing text from two
                        # different columns into one raw block (seen on
                        # a clarifications table: a stray index-column
                        # line bled into a neighbouring answer-column
                        # paragraph). That block's centre point then
                        # lands inside whichever column cell the OTHER,
                        # correctly-segmented block already legitimately
                        # occupies, so both would render into the same
                        # cell and overlap. Once a cell has been claimed
                        # this page, a second block cannot reuse it --
                        # it falls back to its own raw rect instead.
                        already_claimed = any(abs(cell_rect.x0-c.x0)<0.5 and abs(cell_rect.y0-c.y0)<0.5 and abs(cell_rect.x1-c.x1)<0.5 and abs(cell_rect.y1-c.y1)<0.5 for c in claimed_cells)
                        if already_claimed:
                            cell_rect = None
                        else:
                            claimed_cells.append(cell_rect)
                    # True redaction removes the original text operators from
                    # the content stream.  Redact individual glyph-span
                    # bounds, not the whole block: table blocks often include
                    # border geometry in their bounding box.
                    span_rects = []
                    size=10.0; color=(0.0,0.0,0.0); source_font=""; angle=0.0; origin=None
                    # The union of THIS unit's own member blocks, not just
                    # the primary block's own raw text -- a merged multi-
                    # fragment unit's later members would otherwise never
                    # match any span below and would be left un-redacted,
                    # so the old glyphs would still show through underneath
                    # the newly inserted translation.
                    source_text = unit.source_text
                    line_rects: list[tuple[float, object, object]] = []
                    for source_block in source_dict['blocks']:
                        if source_block.get('type') != 0:
                            continue
                        for line in source_block.get('lines', []):
                            line_matched_boxes = []
                            line_origin = None
                            for span in line.get('spans', []):
                                span_text = span.get('text', '').strip()
                                span_box = fitz.Rect(span.get('bbox', ()))
                                # A block's bounding rect can geometrically
                                # overlap a neighbouring, unrelated label on a
                                # dense drawing page (get_text('blocks') and
                                # get_text('dict') do not share block
                                # indices or boundaries). Requiring the span's
                                # own text to actually be part of this
                                # block's source text keeps a wide rect from
                                # picking up another block's font, colour or
                                # rotation angle.
                                if span_text and span_box.intersects(rect) and span_text in source_text:
                                    span_rects.append(span_box + (-0.4, -0.4, 0.4, 0.4))
                                    size=float(span.get('size') or size)
                                    color=self._span_color(span.get('color'))
                                    source_font=str(span.get('font') or source_font)
                                    angle=self._line_angle(line.get('dir'))
                                    if origin is None:
                                        origin = span.get('origin')
                                    if line_origin is None:
                                        line_origin = span.get('origin')
                                    line_matched_boxes.append(span_box)
                            if line_matched_boxes:
                                line_rect = line_matched_boxes[0]
                                for extra in line_matched_boxes[1:]:
                                    line_rect = line_rect | extra
                                line_rects.append((line_rect.y0, line_rect, line_origin))
                    if not span_rects:
                        span_rects=[rect]
                    page_redact_rects.extend(span_rects)
                    line_rects.sort(key=lambda item: item[0])
                    line_origins = [item[2] for item in line_rects]
                    line_rects = [item[1] for item in line_rects]
                    fontfile = _font_file(source_font, text, page=page, prefer_narrow=len(text) >= 80) or (
                        self._cjk_fallback_font() if contains_cjk(text) else r'C:\Windows\Fonts\arial.ttf'
                    )
                    fit_rect = cell_rect or rect
                    if cell_rect is not None:
                        fit_rect = fitz.Rect(fit_rect.x0 + 2, fit_rect.y0 + 2, fit_rect.x1 - 2, fit_rect.y1 - 2)
                    align = self._infer_alignment(page, rect, cell_rect, source_dict, source_text)
                    # Every block keeps its own original rectangle -- no
                    # heuristic ever widens it to borrow page width. A
                    # source label's box, whatever its width, is what the
                    # PDF itself sized it to hold; guessing that a narrow
                    # box is really a wrapped heading fragment stretched it
                    # far past the icon/geometry it belongs next to on a
                    # dense drawing page.
                    plans.append({
                        "page_no": page_no, "block_no": block_no, "rect": rect, "fit_rect": fit_rect,
                        "text": text, "fontfile": fontfile, "size": size, "color": color, "align": align,
                        "angle": angle, "origin": origin, "cell_rect": cell_rect, "page_blocks": page_blocks,
                        "line_rects": line_rects, "line_origins": line_origins,
                    })
                for span_box in page_redact_rects:
                    page.add_redact_annot(span_box, fill=False)
                if page_redact_rects:
                    # images=0: never blank out image pixels under a
                    # redaction box. Only the text content itself is
                    # removed (the default text=0), which is the literal
                    # "delete the original text" this page needs -- this
                    # basemap is a raster image, and the old "erase"
                    # behaviour (image pixels blanked wherever a redaction
                    # rectangle overlapped them) wiped surrounding map
                    # imagery that had nothing to do with the label being
                    # translated. graphics=0 for the same reason on the
                    # vector side: a decorative underline bar drawn as
                    # its own filled rectangle right under a heading
                    # sits fully inside that heading's (small-margin)
                    # redaction box, so the default graphics=1 ("remove
                    # graphics fully contained in the box") silently
                    # deleted it outright rather than just leaving it
                    # the wrong width for the new text -- losing it
                    # entirely is worse than a cosmetic width mismatch.
                    page.apply_redactions(images=0, graphics=0)
                # Pass 2: every original glyph on this page is gone now, so
                # inserting translated text can no longer disturb an
                # unrelated block's redaction.
                for plan in plans:
                    self._insert_plan(page, plan, overflow_warnings)
            # insert_textbox/insert_text embed the ENTIRE requested font
            # file, not just the handful of glyphs actually drawn -- a
            # full CJK font such as SimHei is itself ~10MB, so a page
            # that uses maybe three dozen distinct Chinese characters
            # was otherwise carrying the whole font along for a ~20x
            # file-size increase. subset_fonts() rewrites each embedded
            # font down to only the glyphs the page actually references.
            doc.subset_fonts()
            doc.save(dest, garbage=1, deflate=True)
        finally: doc.close()
        # Re-open the produced file and flag untranslated source glyphs
        # or geometry that escaped the page as warnings rather than
        # discarding the whole document.  This is especially important
        # for table cells: PDF engines may split a cell into several
        # drawing fragments, so a successful API response alone is not
        # proof that every fragment was replaced.
        overflow_warnings.extend(self._validate_output(dest, target_language, expected_image_count=source_image_count))
        return overflow_warnings

    def _insert_plan(self, page, plan, overflow_warnings):
        import fitz
        text = plan["text"]; fontfile = plan["fontfile"]; color = plan["color"]; align = plan["align"]
        fit_rect = plan["fit_rect"]; rect = plan["rect"]; cell_rect = plan["cell_rect"]; page_blocks = plan["page_blocks"]
        block_no = plan["block_no"]; page_no = plan["page_no"]; size = plan["size"]
        # PyMuPDF keys an inserted font resource by its fontname, not its
        # fontfile: reusing a fixed name (for example "F0") across many
        # insert calls on the same page makes every later call silently
        # reuse whichever font was embedded under that name first, even
        # when a different fontfile is requested. A page with hundreds of
        # blocks that mix Latin-only and CJK labels needs a distinct name
        # per actual font file, or a CJK block processed after a Latin-only
        # one renders with the wrong (Latin) font and every CJK glyph comes
        # out blank.
        fontname = self._fontname_for(fontfile)
        if abs(plan["angle"]) > 1.0 and plan["origin"] is not None:
            # Road/feature labels on a drawing page commonly run at an
            # arbitrary angle along the geometry they name. insert_textbox
            # only supports axis-aligned placement, so rotated labels use
            # insert_text with a rotation morph anchored at the original
            # glyph origin instead of the redacted block's rectangle.
            origin = fitz.Point(plan["origin"])
            matrix = fitz.Matrix(1, 1).prerotate(plan["angle"])
            page.insert_text(origin, text, fontsize=size, fontfile=fontfile, fontname=fontname, color=color, morph=(origin, matrix))
            return
        # A block whose own PyMuPDF text still contains several
        # original lines (get_text('blocks') itself sometimes fuses two
        # physically separate, non-stacked labels -- e.g. two different
        # area call-outs -- into one block purely because they share a
        # y-band) is rendered one original line at a time, each into its
        # own original line rectangle, instead of one merged textbox.
        # Stacking every translated line inside the merged block's own
        # bounding rect is what previously pulled a label like the
        # second area call-out away from its real, disjoint position.
        line_rects = plan.get("line_rects") or []
        line_origins = plan.get("line_origins") or []
        parts = text.split("\n")
        if len(line_rects) > 1 and len(parts) == len(line_rects):
            for i, (part, line_rect) in enumerate(zip(parts, line_rects)):
                line_origin = line_origins[i] if i < len(line_origins) else None
                self._fit_and_insert(page, line_rect, part, fontfile, fontname, color, align, plan["size"], page_no, block_no, overflow_warnings, origin=line_origin)
            return
        # Baseline-anchoring assumes the box was sized for ONE physical
        # line, so it can shrink text to fit that single line's width.
        # A source paragraph that was genuinely several wrapped lines
        # (line_rects has more than one entry) but whose translation
        # happens to come back without the original line breaks would
        # otherwise get force-fit onto one line at whatever absurdly
        # small size makes the whole paragraph's width fit -- instead
        # of wrapping across the multiple lines the box was always
        # tall enough for. Only trust the source's own origin when the
        # source really was one line.
        single_line_origin = plan["origin"] if len(line_rects) <= 1 else None
        self._fit_and_insert(page, fit_rect, text, fontfile, fontname, color, align, size, page_no, block_no, overflow_warnings,
                              page_blocks=page_blocks, rect=rect, cell_rect=cell_rect, origin=single_line_origin)

    @staticmethod
    def _measure_textbox(fontfile, fontname, size, text, width, height, align):
        """Render into a disposable scratch page to find the real glyph box.

        insert_textbox's own return value is NOT the leftover vertical
        space in the box: it reserves an internal per-line height that
        is noticeably taller than the visible glyph ink (measured
        empirically at ~1.34x the font size for a CJK font, not the
        font's own ascender-descender span), so neither the return
        value nor the font's raw metrics predict where the ink actually
        lands. Rendering once on a throwaway page and reading back the
        real span boxes is the only reliable way to know.
        """
        import fitz
        scratch = fitz.open()
        page = scratch.new_page(width=width + 20, height=max(height, size * 20) + 20)
        probe_rect = fitz.Rect(10, 10, 10 + width, 10 + height)
        rc = page.insert_textbox(probe_rect, text, fontfile=fontfile, fontsize=size, fontname=fontname, color=(0, 0, 0), align=align, overlay=True)
        boxes = [
            fitz.Rect(span["bbox"])
            for block in page.get_text("dict")["blocks"] if block.get("type") == 0
            for line in block["lines"] for span in line["spans"] if span.get("text", "").strip()
        ]
        scratch.close()
        if rc < 0 or not boxes:
            return None
        total = boxes[0]
        for b in boxes[1:]:
            total = total | b
        # Relative to probe_rect's own top-left, so the caller can add
        # it straight onto their real rect's origin.
        return fitz.Rect(total.x0 - 10, total.y0 - 10, total.x1 - 10, total.y1 - 10)

    @staticmethod
    def _fit_and_insert(page, target_rect, text, fontfile, fontname, color, align, size, page_no, block_no,
                         overflow_warnings, *, page_blocks=None, rect=None, cell_rect=None, origin=None):
        import fitz
        fit_rect = target_rect
        # Anchor single-line text at the ORIGINAL glyph's own baseline
        # instead of trying to fit it inside an abstract box. The box
        # height PyMuPDF reports for the source span reflects the
        # source font's own line-height convention; insert_textbox's
        # internal per-line box reflects a completely different,
        # unrelated convention for whatever substitute font is used
        # here (measured empirically: neither matches the other, and
        # neither matches the font's raw ascender/descender metrics
        # either). No box-centring heuristic reconciles two different
        # fonts' internal metric systems -- but the actual baseline
        # position the original glyph used is a fact recorded directly
        # in the source PDF, and reusing it exactly reproduces the
        # source's vertical position regardless of which font ends up
        # drawing the translation.
        if origin is not None and "\n" not in text:
            font = fitz.Font(fontfile=fontfile) if fontfile else fitz.Font("helv")
            trial_size = size
            width = font.text_length(text, fontsize=trial_size)
            # This project's own readability floor elsewhere is "at
            # least half the source size" -- reuse it here. A single
            # very long translation (the model can return a wrapped
            # source paragraph as one run with no line breaks) would
            # otherwise shrink all the way to the 1pt technical floor
            # just to force its whole width onto one line, producing
            # unreadable text instead of properly wrapping across the
            # box's real height below.
            readable_floor = max(1.0, size * 0.5)
            while trial_size > readable_floor and width > fit_rect.width:
                trial_size = round(trial_size - 0.5, 1)
                width = font.text_length(text, fontsize=trial_size)
            if width <= fit_rect.width + 0.5:
                if align == 1:
                    x = fit_rect.x0 + (fit_rect.width - width) / 2
                elif align == 2:
                    x = fit_rect.x1 - width
                else:
                    x = fit_rect.x0
                page.insert_text(fitz.Point(x, origin[1]), text, fontsize=trial_size, fontfile=fontfile, fontname=fontname, color=color)
                return
            # Too wide even at the floor size (would need real wrapping
            # onto more lines than the source ever had) -- fall through
            # to the box-fit path below, which can grow downward into
            # free space for an extra line.
        # Shrink toward whatever size actually fits; the only floor is a
        # small technical one to stop the search, not an artificial
        # "readability minimum". Forcing a fixed floor larger than a
        # label's own natural size (a tiny map-pin caption can be smaller
        # than any general floor) made insert_textbox silently draw
        # nothing at all once the inflated size could not fit the box.
        measured = None
        while True:
            measured = PdfTranslationService._measure_textbox(fontfile, fontname, size, text, fit_rect.width, fit_rect.height, align)
            if measured is not None: break
            # Use genuine free space below this source block before
            # reducing readability. Never cross the next block on the
            # same horizontal band. Only meaningful for the single
            # whole-block path, which passes the block's own geometry.
            if page_blocks is not None and fit_rect == (cell_rect or rect):
                below = [float(b[1]) for b in page_blocks[block_no + 1:] if float(b[1]) > rect.y1 and float(b[0]) < rect.x1 and float(b[2]) > rect.x0]
                candidate = (min(below) - 1.0) if below else (page.rect.height - 18.0)
                if candidate > rect.y1 + 2.0:
                    fit_rect = fitz.Rect(rect.x0, rect.y0, rect.x1, candidate)
                    continue
            if size <= 1.0:
                break
            size=round(size-0.5,1)
        if measured is None:
            overflow_warnings.append(f"page {page_no+1}, block {block_no}: text may overflow at minimum font size: {text[:80]}")
            final_rect = fit_rect
        elif cell_rect is not None and rect is not None:
            # A detected table cell can be much taller than THIS
            # block's own content -- a short cell sharing a row with a
            # much longer neighbouring cell gets the whole row's
            # height as its cell_rect. Vertically centring inside that
            # full row height pulls a short cell's text down into the
            # middle of the row instead of the top, where the source
            # content (and every ordinary table convention) actually
            # starts it. Align to the top of the block's OWN natural
            # rect, not the centre of the shared cell.
            shift = rect.y0 - fit_rect.y0 - measured.y0
            final_rect = fitz.Rect(fit_rect.x0, fit_rect.y0 + shift, fit_rect.x1, fit_rect.y1 + shift)
        else:
            # Shift the whole box (not just pad its edges) so the
            # measured glyph box's own centre lands on fit_rect's centre
            # -- this is the real fix for translated text rendering
            # higher than the source glyph did: the earlier version
            # centred insert_textbox's internal line-box, not the
            # visible ink, and those two are not the same thing.
            shift = fit_rect.height / 2 - (measured.y0 + measured.y1) / 2
            final_rect = fitz.Rect(fit_rect.x0, fit_rect.y0 + shift, fit_rect.x1, fit_rect.y1 + shift)
        page.insert_textbox(final_rect,text,fontfile=fontfile,fontsize=size,fontname=fontname,color=color,align=align,overlay=True)

    @staticmethod
    def _fontname_for(fontfile: str | None) -> str:
        if not fontfile:
            return "helv"
        return "PT_" + re.sub(r"[^A-Za-z0-9]", "", Path(fontfile).stem)[:20]

    @staticmethod
    def _cjk_fallback_font() -> str:
        """Prefer Source Han Sans over the bundled Windows SimHei fallback.

        SimHei's glyph coverage is narrower than a modern Han Sans build and
        it visibly boxes some punctuation used in translated captions. A
        prior BabelDOC run already cached Source Han Sans CN locally as a
        translation asset; reuse that file when present without adding a
        babeldoc dependency to this fallback path, and fall back to SimHei
        only if that cache is missing.
        """
        cached = Path.home() / ".cache" / "babeldoc" / "fonts" / "SourceHanSansCN-Regular.ttf"
        if cached.is_file():
            return str(cached)
        return r'C:\Windows\Fonts\simhei.ttf'

    @staticmethod
    def _span_color(value: object) -> tuple[float, float, float]:
        try:
            raw = int(value)
        except (TypeError, ValueError):
            return (0.0, 0.0, 0.0)
        if raw == -1:
            # PyMuPDF reports -1 (all 32 bits set) when the PDF never set an
            # explicit colour for the span, not a request for literal white
            # text. Bit-masking that sentinel to 0xFFFFFF would silently
            # render the replacement text invisible on a light background.
            return (0.0, 0.0, 0.0)
        packed = raw & 0xFFFFFF
        return ((packed >> 16) / 255.0, ((packed >> 8) & 0xFF) / 255.0, (packed & 0xFF) / 255.0)

    @staticmethod
    def _line_angle(direction: object) -> float:
        """Return the line's rotation in degrees, 0 for normal left-to-right text."""
        if not isinstance(direction, (tuple, list)) or len(direction) != 2:
            return 0.0
        dx, dy = float(direction[0]), float(direction[1])
        if dx == 0 and dy == 0:
            return 0.0
        return -math.degrees(math.atan2(dy, dx))

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
    def _infer_alignment(page, source_rect, cell_rect, source_dict, source_text=""):
        """Infer left/center/right alignment from the original glyph bounds."""
        import fitz
        lines = []
        for block in source_dict.get('blocks', []):
            if block.get('type') != 0:
                continue
            for line in block.get('lines', []):
                line_boxes = []
                for span in line.get('spans', []):
                    span_text = span.get('text', '').strip()
                    box = fitz.Rect(span.get('bbox', ()))
                    # Legend/label rows on a dense drawing page often sit
                    # back-to-back with zero gap, so a neighbouring row's
                    # span box can geometrically touch this block's rect
                    # even though it belongs to a different line entirely
                    # (see the identical guard on the redaction-rect scan
                    # above). Requiring the span's own text to actually be
                    # part of this block's source text keeps that
                    # neighbour from being counted as a second "line" of
                    # this block, which would otherwise make a lone
                    # single-line legend entry look like genuine multi-line
                    # text and defeat the single-line left-align default.
                    if span_text and box.intersects(source_rect) and (not source_text or span_text in source_text):
                        line_boxes.append(box)
                if line_boxes:
                    lines.append((min(b.x0 for b in line_boxes), max(b.x1 for b in line_boxes)))
        if not lines:
            return 0
        if len(lines) < 2 and cell_rect is None:
            # Alignment is a multi-line pattern: a lone single-line block
            # (the common case for scattered drawing/map labels) has no
            # real container to be left/right-aligned within, and
            # comparing its one line against the whole page edges only
            # reports whichever half of the page it happens to sit on --
            # e.g. every right-column legend entry looks right-aligned
            # purely because the legend sits on the right side of the
            # page, even though every entry is actually flush-left
            # against its own colour swatch. Keep the natural left
            # position in that case; only a real container (a table
            # cell) or genuine multi-line text justifies inferring a
            # centre/right alignment.
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
    def _validate_output(path: Path, target_language: str, *, expected_image_count: int | None = None) -> list[str]:
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
            return list(dict.fromkeys(failures))
        finally:
            doc.close()
