"""Hybrid PDF parsing for geometry-safe translation.

PyMuPDF is the authoritative source for page geometry, font information and
text direction.  MinerU 4 is an optional semantic hint source.  The parser
does not invoke a model or download assets implicitly; a MinerU result can be
provided explicitly, or through ``DOCUMENT_TRANSLATOR_MINERU_OUTPUT``.

The output is deliberately small and auditable.  Stable line IDs are derived
from the source hash and native PDF coordinates, while semantic boundaries are
kept separate from the geometry records used for writeback.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping


_TEXT_BLOCK = 0
_WHITESPACE_RE = re.compile(r"\s+")
_STRUCTURAL_START_RE = re.compile(
    r"^(?:chapter|part|section|article|clause|appendix|table|figure|"
    r"\d+[.)]|[A-Z][.)]|[-*•])",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class PdfNativeLine:
    id: str
    page: int
    block: int
    line: int
    text: str
    bbox: tuple[float, float, float, float]
    direction: tuple[float, float]
    angle: float
    fontsize: float
    fonts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PdfSemanticBlock:
    id: str
    page: int
    order: int
    text: str
    kind: str
    bbox: tuple[float, float, float, float] | None
    line_ids: tuple[str, ...]
    source: str


@dataclass(frozen=True, slots=True)
class PdfHybridParse:
    source_hash: str
    parser: str
    mineru_status: str
    page_count: int
    native_lines: tuple[PdfNativeLine, ...]
    semantic_blocks: tuple[PdfSemanticBlock, ...]
    rotated_pages: tuple[int, ...]
    table_pages: tuple[int, ...]

    @property
    def boundary_map(self) -> dict[str, tuple[dict[str, object], ...]]:
        """Return explicit semantic block starts grouped by one-based page."""
        result: dict[str, list[dict[str, object]]] = {}
        previous_by_page: dict[int, PdfSemanticBlock] = {}
        for block in self.semantic_blocks:
            previous = previous_by_page.get(block.page)
            if previous is not None and block.line_ids:
                result.setdefault(str(block.page), []).append(
                    {
                        "text": block.text,
                        "first_line_text": block.text.splitlines()[0],
                        "kind": block.kind,
                        "bbox": list(block.bbox) if block.bbox else None,
                        "line_id": block.line_ids[0],
                    }
                )
            previous_by_page[block.page] = block
        return {page: tuple(items) for page, items in result.items()}

    def manifest(self) -> dict[str, object]:
        return {
            "schema": "document-translator.pdf-hybrid",
            "schema_version": 1,
            "source_hash": self.source_hash,
            "parser": self.parser,
            "mineru_status": self.mineru_status,
            "page_count": self.page_count,
            "rotated_pages": list(self.rotated_pages),
            "table_pages": list(self.table_pages),
            "native_lines": [asdict(line) for line in self.native_lines],
            "semantic_blocks": [asdict(block) for block in self.semantic_blocks],
            "boundary_map": self.boundary_map,
        }

    def write_manifest(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(self.manifest(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return destination


def parse_pdf(
    path: str | Path,
    *,
    parser: str = "auto",
    mineru_output: str | Path | None = None,
) -> PdfHybridParse:
    """Extract native geometry and optional MinerU semantic structure.

    ``auto`` uses an explicitly supplied MinerU result when available and
    otherwise uses the deterministic native semantic grouping.  ``native``
    never reads MinerU.  ``mineru`` requires a result path and fails closed if
    it cannot be read; it is intended for controlled resource-preparation
    runs, not for implicit model downloads during translation.
    """
    mode = str(parser or "auto").casefold()
    if mode not in {"auto", "native", "mineru"}:
        raise ValueError("PDF semantic parser must be one of: auto, native, mineru")
    source = Path(path).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    source_hash = _sha256(source)

    native_lines, page_sizes, table_pages = _extract_native(source, source_hash)
    native_blocks = _native_semantic_blocks(native_lines, page_sizes)
    mineru_status = "not_requested"
    semantic_blocks = native_blocks
    explicit_output = mineru_output or os.environ.get("DOCUMENT_TRANSLATOR_MINERU_OUTPUT")
    if mode in {"auto", "mineru"} and explicit_output:
        payload = _load_mineru_output(explicit_output)
        mineru_blocks = _mineru_semantic_blocks(payload, native_lines, page_sizes)
        if not mineru_blocks:
            if mode == "mineru":
                raise ValueError("MinerU output contains no usable semantic blocks")
            mineru_status = "invalid_fallback_native"
        else:
            semantic_blocks = mineru_blocks
            mineru_status = "loaded"
    elif mode == "mineru":
        raise ValueError(
            "semantic parser 'mineru' requires --mineru-output or "
            "DOCUMENT_TRANSLATOR_MINERU_OUTPUT"
        )
    elif mode == "auto":
        mineru_status = "not_configured_fallback_native"

    rotated_pages = tuple(
        sorted({line.page for line in native_lines if not _is_axis_aligned(line.direction)})
    )
    return PdfHybridParse(
        source_hash=source_hash,
        parser="mineru" if mineru_status == "loaded" else "native",
        mineru_status=mineru_status,
        page_count=len(page_sizes),
        native_lines=tuple(native_lines),
        semantic_blocks=tuple(semantic_blocks),
        rotated_pages=rotated_pages,
        table_pages=tuple(table_pages),
    )


def load_manifest(path: str | Path) -> dict[str, object]:
    """Load and minimally validate a worker-side hybrid parse manifest."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != "document-translator.pdf-hybrid":
        raise ValueError("invalid PDF hybrid parse manifest")
    return payload


def _extract_native(
    source: Path,
    source_hash: str,
) -> tuple[list[PdfNativeLine], list[tuple[float, float]], list[int]]:
    try:
        import fitz
    except ImportError as exc:  # pragma: no cover - deployment dependent
        raise RuntimeError("PDF hybrid parsing requires PyMuPDF") from exc
    document = fitz.open(source)
    lines: list[PdfNativeLine] = []
    page_sizes: list[tuple[float, float]] = []
    table_pages: list[int] = []
    try:
        for page_number, page in enumerate(document, 1):
            page_sizes.append((float(page.rect.width), float(page.rect.height)))
            if _looks_like_table(page):
                table_pages.append(page_number)
            for block_index, block in enumerate(page.get_text("dict").get("blocks", [])):
                if block.get("type") != _TEXT_BLOCK:
                    continue
                for line_index, line in enumerate(block.get("lines", [])):
                    spans = [
                        span for span in line.get("spans", [])
                        if str(span.get("text", "")).strip()
                    ]
                    text = "".join(str(span.get("text", "")) for span in spans).strip()
                    if not text:
                        continue
                    boxes = [tuple(float(value) for value in span["bbox"]) for span in spans]
                    bbox = (
                        min(box[0] for box in boxes),
                        min(box[1] for box in boxes),
                        max(box[2] for box in boxes),
                        max(box[3] for box in boxes),
                    )
                    direction = tuple(float(value) for value in line.get("dir", (1.0, 0.0)))
                    fontsize = max(float(span.get("size", 0) or 0) for span in spans)
                    fonts = tuple(dict.fromkeys(str(span.get("font", "")) for span in spans))
                    identity = json.dumps(
                        [source_hash, page_number, block_index, line_index, text, [round(v, 3) for v in bbox]],
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    lines.append(
                        PdfNativeLine(
                            id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                            page=page_number,
                            block=block_index,
                            line=line_index,
                            text=text,
                            bbox=bbox,
                            direction=(direction[0], direction[1]),
                            angle=math.degrees(math.atan2(-direction[1], direction[0])),
                            fontsize=fontsize,
                            fonts=fonts,
                        )
                    )
    finally:
        document.close()
    return lines, page_sizes, table_pages


def _native_semantic_blocks(
    lines: Iterable[PdfNativeLine],
    page_sizes: list[tuple[float, float]],
) -> list[PdfSemanticBlock]:
    by_page: dict[int, list[PdfNativeLine]] = {}
    for line in lines:
        by_page.setdefault(line.page, []).append(line)
    blocks: list[PdfSemanticBlock] = []
    for page, page_lines in by_page.items():
        ordered = _reading_order(page_lines, page_sizes[page - 1][0])
        current: list[PdfNativeLine] = []
        for line in ordered:
            if current and _starts_new_block(current[-1], line):
                blocks.append(_make_native_block(page, len(blocks), current))
                current = []
            current.append(line)
        if current:
            blocks.append(_make_native_block(page, len(blocks), current))
    return blocks


def _reading_order(lines: list[PdfNativeLine], page_width: float) -> list[PdfNativeLine]:
    if not lines:
        return []
    # A large horizontal gap between line centers is a conservative two-column
    # signal.  Do not merge columns even when their vertical ranges overlap.
    centers = sorted((line.bbox[0] + line.bbox[2]) / 2 for line in lines)
    gaps = [right - left for left, right in zip(centers, centers[1:])]
    column_gap = max(36.0, page_width * 0.12)
    split_at = next((index + 1 for index, gap in enumerate(gaps) if gap > column_gap), None)
    if split_at is None or len(lines) < 6:
        return sorted(lines, key=lambda item: (round(item.bbox[1], 2), item.bbox[0]))
    pivot = (centers[split_at - 1] + centers[split_at]) / 2
    left = [line for line in lines if (line.bbox[0] + line.bbox[2]) / 2 <= pivot]
    right = [line for line in lines if line not in left]
    return sorted(left, key=lambda item: (item.bbox[1], item.bbox[0])) + sorted(
        right, key=lambda item: (item.bbox[1], item.bbox[0])
    )


def _starts_new_block(previous: PdfNativeLine, current: PdfNativeLine) -> bool:
    previous_height = max(1.0, previous.bbox[3] - previous.bbox[1])
    gap = current.bbox[1] - previous.bbox[3]
    if _STRUCTURAL_START_RE.match(previous.text):
        return True
    if gap > max(8.0, previous_height * 1.65):
        return True
    if _STRUCTURAL_START_RE.match(current.text):
        return True
    if not _same_column(previous, current):
        return True
    return False


def _same_column(left: PdfNativeLine, right: PdfNativeLine) -> bool:
    overlap = min(left.bbox[2], right.bbox[2]) - max(left.bbox[0], right.bbox[0])
    return overlap > -max(left.bbox[2] - left.bbox[0], right.bbox[2] - right.bbox[0]) * 0.25


def _make_native_block(page: int, order: int, lines: list[PdfNativeLine]) -> PdfSemanticBlock:
    text = "\n".join(line.text for line in lines)
    bbox = (
        min(line.bbox[0] for line in lines),
        min(line.bbox[1] for line in lines),
        max(line.bbox[2] for line in lines),
        max(line.bbox[3] for line in lines),
    )
    kind = "heading" if _STRUCTURAL_START_RE.match(lines[0].text) else "paragraph"
    identity = f"native:{page}:{order}:{','.join(line.id for line in lines)}"
    return PdfSemanticBlock(
        id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        page=page,
        order=order,
        text=text,
        kind=kind,
        bbox=bbox,
        line_ids=tuple(line.id for line in lines),
        source="pymupdf",
    )


def _mineru_semantic_blocks(
    payload: Mapping[str, Any],
    native_lines: list[PdfNativeLine],
    page_sizes: list[tuple[float, float]],
) -> list[PdfSemanticBlock]:
    pages = payload.get("pages")
    if not isinstance(pages, list):
        return []
    native_by_page: dict[int, list[PdfNativeLine]] = {}
    for line in native_lines:
        native_by_page.setdefault(line.page, []).append(line)
    blocks: list[PdfSemanticBlock] = []
    for raw_page in pages:
        if not isinstance(raw_page, Mapping):
            continue
        page = int(raw_page.get("page_idx", raw_page.get("page", -1))) + (
            1 if "page_idx" in raw_page else 0
        )
        if page < 1 or page > len(page_sizes):
            continue
        for raw_block in raw_page.get("blocks", []):
            if not isinstance(raw_block, Mapping):
                continue
            text = _mineru_text(raw_block).strip()
            if not text:
                continue
            bbox = _mineru_bbox(raw_block, page_sizes[page - 1])
            kind = str(raw_block.get("type", raw_block.get("category", "text"))).casefold()
            matched = _match_native_lines(text, bbox, native_by_page.get(page, []))
            identity = f"mineru:{page}:{len(blocks)}:{text}:{bbox}"
            blocks.append(
                PdfSemanticBlock(
                    id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                    page=page,
                    order=len(blocks),
                    text=text,
                    kind=kind,
                    bbox=bbox,
                    line_ids=tuple(line.id for line in matched),
                    source="mineru",
                )
            )
    return blocks


def _load_mineru_output(value: str | Path) -> Mapping[str, Any]:
    path = Path(value)
    if path.is_dir():
        candidates = (
            path / "middle_json.json",
            path / "structured_content.json",
            path / "content_list.json",
            path / "model_output.json",
        )
        path = next((candidate for candidate in candidates if candidate.is_file()), path)
    if not path.is_file():
        raise FileNotFoundError(f"MinerU output not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("MinerU output must be a JSON object")
    return payload


def _mineru_text(block: Mapping[str, Any]) -> str:
    content = block.get("content", block.get("text", ""))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, Mapping):
                pieces.append(str(item.get("content", item.get("text", ""))))
        return "".join(pieces)
    return str(content or "")


def _mineru_bbox(block: Mapping[str, Any], page_size: tuple[float, float]) -> tuple[float, float, float, float] | None:
    value = block.get("bbox", block.get("box"))
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    numbers = tuple(float(item) for item in value)
    # Content-list bboxes may be normalized to 0..1000.  MiddleJson bboxes
    # normally use PDF points; distinguish them from real page coordinates.
    if max(numbers) <= 1000 and max(page_size) > 1100:
        width, height = page_size
        return (
            numbers[0] * width / 1000,
            numbers[1] * height / 1000,
            numbers[2] * width / 1000,
            numbers[3] * height / 1000,
        )
    return numbers  # type: ignore[return-value]


def _match_native_lines(text: str, bbox: tuple[float, float, float, float] | None, lines: list[PdfNativeLine]) -> list[PdfNativeLine]:
    compact = _compact(text)
    candidates = lines
    if bbox is not None:
        candidates = [line for line in lines if _intersection_over_union(line.bbox, bbox) > 0.01]
    matched: list[PdfNativeLine] = []
    joined = ""
    for line in sorted(candidates, key=lambda item: (item.bbox[1], item.bbox[0])):
        trial = _compact(joined + line.text)
        if compact.startswith(trial) or trial.startswith(compact) or compact in trial:
            matched.append(line)
            joined += line.text
            if compact == _compact(joined):
                break
    return matched


def _intersection_over_union(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    return intersection / max(1e-6, left_area + right_area - intersection)


def _compact(value: str) -> str:
    return _WHITESPACE_RE.sub("", value).casefold()


def _is_axis_aligned(direction: tuple[float, float]) -> bool:
    dx, dy = direction
    return abs(dx) <= 0.05 or abs(dy) <= 0.05


def _looks_like_table(page: Any) -> bool:
    width, height = float(page.rect.width), float(page.rect.height)
    horizontal = vertical = 0
    for drawing in page.get_drawings():
        for item in drawing.get("items", []):
            if not item:
                continue
            if item[0] == "l" and len(item) >= 3:
                first, second = item[1], item[2]
                dx, dy = abs(float(second.x) - float(first.x)), abs(float(second.y) - float(first.y))
                if dy <= 1.5 and dx >= width * 0.35:
                    horizontal += 1
                elif dx <= 1.5 and dy >= height * 0.15:
                    vertical += 1
            elif item[0] == "re" and len(item) >= 2:
                rect = item[1]
                rw, rh = abs(float(rect.x1) - float(rect.x0)), abs(float(rect.y1) - float(rect.y0))
                if rw >= width * 0.35 and rh <= 2:
                    horizontal += 1
                elif rh >= height * 0.15 and rw <= 2:
                    vertical += 1
    return horizontal >= 3 and vertical >= 2


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
