"""PDF text extraction and geometry-preserving rewrite using PyMuPDF."""
from __future__ import annotations
import hashlib
import re
from pathlib import Path
from document_translator.core import DocumentFormat, DocumentLocation, TranslationUnit, generate_unit_id
from document_translator.translation_rules import rule_protected_tokens

def read_pdf(path: str | Path, *, source_language: str = "auto", target_language: str = "en") -> tuple[TranslationUnit, ...]:
    try:
        import fitz
    except ImportError as exc:
        raise RuntimeError("PDF support requires PyMuPDF; install the PDF runtime first") from exc
    source = Path(path); doc = fitz.open(source); digest = hashlib.sha256(source.read_bytes()).hexdigest(); units=[]
    try:
        for page_no, page in enumerate(doc):
            blocks = [b for b in page.get_text("blocks") if int(b[6]) == 0 and str(b[4]).strip()]
            groups = _paragraph_groups(page, blocks)
            for group_no, group in enumerate(groups):
                text = "\n".join(str(b[4]).strip() for b in group).strip()
                if not text.strip(): continue
                data=dict(document_hash=digest, format=DocumentFormat.PDF,
                    location=DocumentLocation(part=f"page:{page_no+1}", object_id=f"para:{group_no}", node_ids=[f"block:{blocks.index(b)}" for b in group]),
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
        return [[b] for b in blocks]
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
        # A paragraph may legitimately change indentation after its first
        # line. Use semantic boundaries instead of a fixed left-edge test.
        if gap <= 35 and not starts_clause and not starts_title and not boundary_hint:
            groups[-1].append(block)
        else:
            groups.append([block])
    return groups
