"""PDF text extraction and geometry-preserving rewrite using PyMuPDF."""
from __future__ import annotations
import hashlib
import re
from pathlib import Path
from document_translator.core import DocumentFormat, DocumentLocation, TranslationUnit, generate_unit_id
from document_translator.translation_rules import rule_protected_tokens

# See the comment at its use in _paragraph_groups() below for why this caps
# placeholder density rather than a fixed batch size like the 24-unit
# per-request chunking in mineru_pdf.py's table path; the two are unrelated
# limits that happen to share a magnitude.
_MAX_GROUP_PROTECTED_TOKENS = 24


def _looks_like_language_text(text: str) -> bool:
    """Reject content that is only punctuation, digits or symbol-font glyphs.

    A dense drawing page can carry marker/icon fonts (for example Esri's
    "ESRIDefaultMarker") whose character codes extract as a couple of
    ordinary-looking punctuation characters even though the glyph actually
    rendered is a map pin or symbol, not text. Sending that through
    translation wastes a request and then has nothing meaningful to
    redact-and-reinsert; leaving the block untouched keeps the original
    icon intact.
    """
    return any(char.isalpha() for char in text) or any("一" <= char <= "鿿" for char in text)

def read_pdf(path: str | Path, *, source_language: str = "auto", target_language: str = "en") -> tuple[TranslationUnit, ...]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF support requires PyMuPDF; install the PDF runtime first") from exc
    source = Path(path); doc = fitz.open(source); digest = hashlib.sha256(source.read_bytes()).hexdigest(); units=[]
    try:
        for page_no, page in enumerate(doc):
            blocks = [b for b in page.get_text("blocks") if int(b[6]) == 0 and str(b[4]).strip() and _looks_like_language_text(str(b[4]))]
            groups = _paragraph_groups(page, blocks)
            for group_no, group in enumerate(groups):
                text = "\n".join(str(b[4]).strip() for b in group).strip()
                if not text.strip(): continue
                data=dict(document_hash=digest, format=DocumentFormat.PDF,
                    # PyMuPDF's own block tuple carries its true index at
                    # b[5]; using that (rather than the position of b within
                    # this possibly-filtered `blocks` list) keeps node_ids
                    # aligned with a fresh, unfiltered page.get_text("blocks")
                    # call in the rewrite step.
                    location=DocumentLocation(part=f"page:{page_no+1}", object_id=f"para:{group_no}", node_ids=[f"block:{int(b[5])}" for b in group]),
                    source_language=source_language,target_language=target_language,source_text=text,
                    protected_tokens=rule_protected_tokens(text),style_signature="",context_before="",context_after="")
                units.append(TranslationUnit(id=generate_unit_id(**data),**data))
    finally: doc.close()
    return tuple(units)


def _paragraph_groups(page, blocks):
    """Merge line fragments into paragraphs while keeping table cells intact."""
    drawings = page.get_drawings()
    horizontal = [d for d in drawings if abs(float(d['rect'].y1)-float(d['rect'].y0)) < .5 and float(d['rect'].width) > 80]
    is_table = len(horizontal) >= 3
    if is_table:
        return _merge_overlapping_label_fragments(blocks)
    font_sizes = _block_font_sizes(page, blocks)
    groups = []
    page_height = float(page.rect.height)
    for block in blocks:
        text = str(block[4]).strip()
        # Page numbers and footer material are independent structural nodes.
        if float(block[1]) > page_height * 0.82:
            groups.append([block])
            continue
        if not groups:
            groups.append([block]); continue
        prev = groups[-1][-1]
        if float(prev[1]) > page_height * 0.82:
            groups.append([block]); continue
        gap = float(block[1]) - float(prev[3])
        starts_clause = bool(re.match(r"^(第[一二三四五六七八九十百0-9]+章|第[一二三四五六七八九十百0-9]+条|Chapter\s+\d+|Article\s+\d+)", text, re.I))
        starts_title = bool(re.match(r"^(表\s*\d+|Table\s+\d+)", text, re.I))
        boundary_hint = str(prev[4]).strip().endswith((":", "：")) or bool(re.match(r"^\d{4}\s*年", text))
        # A title page commonly stacks several distinct heading lines
        # (main title, project number, date...) only a few points apart
        # -- well inside the 35pt gap allowed for a wrapped paragraph --
        # but each line is its own semantic unit set at its OWN font
        # size, unlike a genuine wrapped paragraph where every line
        # shares one size. Without this check every heading on a title
        # page was fused into a single giant paragraph.
        same_font = abs(font_sizes.get(block[5], 0.0) - font_sizes.get(prev[5], 0.0)) <= 0.5
        # A paragraph may legitimately change indentation after its first
        # line. Use semantic boundaries instead of a fixed left-edge test.
        if gap <= 35 and same_font and not starts_clause and not starts_title and not boundary_hint:
            # A dense, unruled data table (no drawn cell borders, so
            # `is_table` above never trips) still passes every prose
            # heuristic here: short, same-size, tightly-spaced lines of
            # figures read exactly like a wrapped paragraph. Each bare
            # number in it is a protected placeholder by design (see
            # translation_rules.rule_protected_tokens), which is correct
            # for an ordinary paragraph's occasional figure -- but a
            # numbers-only table row-by-row fuses into a block that is
            # almost entirely placeholders (one real case hit 200+ in a
            # single unit). A provider cannot reliably reproduce that
            # many placeholders verbatim in one response; observed
            # effect was a multi-minute stall ending in a rejected
            # translation, leaving the whole block untranslated. Capping
            # placeholder density -- not text length, since an ordinary
            # long paragraph with few numbers is harmless -- keeps every
            # genuine paragraph merge intact and only splits this table
            # case back into its natural per-row units.
            candidate = "\n".join(str(b[4]).strip() for b in groups[-1]) + "\n" + text
            if len(rule_protected_tokens(candidate)) <= _MAX_GROUP_PROTECTED_TOKENS:
                groups[-1].append(block)
            else:
                groups.append([block])
        else:
            groups.append([block])
    return groups


def _block_font_sizes(page, blocks):
    """Look up each block's own font size via a single get_text('dict') pass."""
    import fitz
    sizes: dict[int, float] = {}
    dict_blocks = page.get_text('dict').get('blocks', [])
    for block in blocks:
        rect = fitz.Rect(block[:4])
        text = str(block[4])
        found = 0.0
        for source_block in dict_blocks:
            if source_block.get('type') != 0:
                continue
            for line in source_block.get('lines', []):
                for span in line.get('spans', []):
                    span_text = span.get('text', '').strip()
                    if span_text and span_text in text and fitz.Rect(span.get('bbox', ())).intersects(rect):
                        found = float(span.get('size') or 0.0)
                        break
                if found:
                    break
            if found:
                break
        sizes[int(block[5])] = found
    return sizes


_DS_SUFFIX_ONLY_RE = re.compile(r"^(?:[A-Za-z]+\s+){0,2}DS\s*$")


def _merge_overlapping_label_fragments(blocks):
    """Reunite a pin caption whose DS-suffix line PyMuPDF split off on its own.

    Most multi-line drawing-page captions ("CENTER\nPOINT DS") come back
    from get_text('blocks') as a single block with an embedded newline,
    but an occasional one ("ABDUL REHMAN" / "ROAD DS") comes back as two
    separate, geometrically overlapping blocks instead. Left unmerged,
    the trailing fragment is translated with no idea it continues the
    preceding line's place name.

    This page is dense with short, closely packed labels and a real
    title-block table, so a generic "these two boxes touch" rule merges
    plenty of unrelated neighbours too (two different pin captions, or
    adjacent table cells that happen to share a column). Scoping the
    match to only a fragment whose ENTIRE own text is the bare "<0-2
    words> DS" pattern the project's own DS-suffix naming rule already
    looks for keeps this to genuine continuations: nothing else on this
    page (an unrelated caption, a table header) happens to consist of
    only that.
    """
    import fitz
    groups: list[list] = []
    for block in blocks:
        rect = fitz.Rect(block[:4])
        text = str(block[4]).strip()
        merged = False
        if groups and rect.height <= 20 and _DS_SUFFIX_ONLY_RE.match(text):
            last_rect = fitz.Rect(groups[-1][-1][:4])
            if last_rect.height <= 20 and last_rect.intersects(rect) and (
                _contains_x_range(rect, last_rect) or _contains_x_range(last_rect, rect)
            ):
                groups[-1].append(block)
                merged = True
        if not merged:
            groups.append([block])
    return groups


def _contains_x_range(inner, outer, tolerance: float = 1.5) -> bool:
    return inner.x0 >= outer.x0 - tolerance and inner.x1 <= outer.x1 + tolerance
