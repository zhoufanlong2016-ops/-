"""Automatic checks on a translated PDF (en -> zh)."""
import json
import re
import sys

import fitz

from document_translator.services import pdf_inplace, pdf_table

WORDS = re.compile(r"(?:\b[A-Za-z][a-z]{2,}\b[ ,]+){4,}[A-Za-z][a-z]{2,}")


def check(source: str, output: str) -> dict:
    src, out = fitz.open(source), fitz.open(output)
    issues: dict[str, list] = {"english": [], "cell_overflow": [], "half_empty_continued": [], "lost_pages": []}
    # 1. English left where the source had translatable text
    for number, page in enumerate(out, start=1):
        if not src[number - 1].get_text().strip():
            continue
        drawings = len(src[number - 1].get_drawings())
        if drawings > 3000:  # CAD drawing sheets: labels are kept by design
            continue
        for block in page.get_text("dict").get("blocks", ()):
            for line in block.get("lines", ()):
                text = "".join(s["text"] for s in line["spans"])
                if WORDS.search(text) and not re.search(r"[一-鿿]", text):
                    issues["english"].append((number, text.strip()[:70]))
    # 2/3. tables
    tables = pdf_table.extract_pdf_tables(source)
    cells = {c.id: c for t in tables for c in t.cells}
    for t in tables:
        if pdf_inplace._is_drawing_frame(t, src[t.page_number - 1]) or src[t.page_number - 1].rotation:
            continue
        page = out[t.page_number - 1]
        for c in t.cells:
            if c.rect is None or c.is_empty:
                continue
            rect = fitz.Rect(c.rect)
            for block in page.get_text("dict", clip=rect).get("blocks", ()):
                for line in block.get("lines", ()):
                    box = fitz.Rect(line["bbox"])
                    if re.search(r"[一-鿿]", "".join(s["text"] for s in line["spans"])) and box.y1 > rect.y1 + 2:
                        issues["cell_overflow"].append((t.page_number, c.id, round(box.y1 - rect.y1, 1)))
    for head, tail in pdf_inplace._continued_cells(tables).items():
        c = cells[head]
        page = out[c.page_number - 1]
        lines = [l for b in page.get_text("dict", clip=fitz.Rect(c.rect)).get("blocks", ()) for l in b.get("lines", ())]
        rest = out[tail.page_number - 1].get_textbox(fitz.Rect(tail.rect)).strip()
        if lines and rest:
            free = c.rect[3] - max(l["bbox"][3] for l in lines)
            size = lines[-1]["spans"][0]["size"]
            if free > 2.2 * size:
                issues["half_empty_continued"].append((c.page_number, head, round(free)))
    # 4. a page that had text but lost all of it
    for number, page in enumerate(out, start=1):
        if src[number - 1].get_text().strip() and not page.get_text().strip():
            issues["lost_pages"].append(number)
    return issues


if __name__ == "__main__":
    result = check(sys.argv[1], sys.argv[2])
    print(json.dumps({k: (len(v), v[:6]) for k, v in result.items()}, ensure_ascii=False))
