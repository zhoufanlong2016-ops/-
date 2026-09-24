"""Generic structural and layout contracts for translated PDFs.

The PDF worker is intentionally responsible for the difficult general PDF
reconstruction problem.  This module handles the small, deterministic layer
that a translation model cannot own: structural ordinals, document-reference
fields, source alignment anchors, and safe reflow of short semantic blocks.

No source document text is used as a production rule.  A source PDF is first
classified into roles and geometry; the target-language style is supplied by a
profile and can be replaced without changing the recogniser.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Mapping


_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_NUMBERED_SOURCE_RE = re.compile(
    r"第\s*(?P<number>[〇零一二三四五六七八九十百千万亿两\d]+)\s*"
    r"(?P<kind>章|条|节|款|项)\s*(?P<body>.*)",
    re.S,
)
_DOCUMENT_REFERENCE_RE = re.compile(
    r"(?P<prefix>[\u3400-\u9fffA-Za-z0-9·._-]{2,})\s*"
    r"(?P<open>[〔\[\(（])\s*(?P<year>\d{2,4})\s*"
    r"(?P<close>[〕\]\)）])\s*(?P<serial>\d+)\s*(?P<marker>号|No\.?|号令)?",
)
_TARGET_LABEL_RE = re.compile(
    r"^\s*(?P<label>chapter|part|section|article|clause|item)\s+"
    r"(?P<number>[0-9]+|[ivxlcdm]+|[a-z]+)(?P<rest>.*)$",
    re.I | re.S,
)
_SOFT_HYPHEN_RE = re.compile(r"[\u00ad\u200b]")
_WHITESPACE_RE = re.compile(r"\s+")
_UNSAFE_TEXT_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_CN_DIGITS = {
    "〇": 0,
    "零": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}
_CN_SMALL_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_LARGE_UNITS = {"万": 10_000, "亿": 100_000_000}


@dataclass(frozen=True, slots=True)
class NumberingProfile:
    """Target-language rendering policy for structural labels.

    The defaults are a neutral English profile, not a universal legal rule.
    Projects can replace it with a JSON profile, for example by choosing
    ``chapter.numbering = "roman"`` or ``article.label = "Clause"``.
    """

    target_language: str = "en"
    chapter_label: str = "Chapter"
    article_label: str = "Article"
    chapter_numbering: str = "arabic"
    article_numbering: str = "arabic"
    separator: str = " "
    reference_open: str = "["
    reference_close: str = "]"
    reference_marker: str = "No."

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, Any],
        *,
        target_language: str,
    ) -> "NumberingProfile":
        chapter = mapping.get("chapter", {})
        article = mapping.get("article", {})
        reference = mapping.get("document_reference", {})
        if not isinstance(chapter, Mapping) or not isinstance(article, Mapping):
            raise ValueError("chapter and article profile entries must be objects")
        if not isinstance(reference, Mapping):
            raise ValueError("document_reference profile entry must be an object")
        values = {
            "target_language": str(mapping.get("target_language", target_language)),
            "chapter_label": str(chapter.get("label", cls.chapter_label)),
            "article_label": str(article.get("label", cls.article_label)),
            "chapter_numbering": str(chapter.get("numbering", cls.chapter_numbering)).casefold(),
            "article_numbering": str(article.get("numbering", cls.article_numbering)).casefold(),
            "separator": str(mapping.get("separator", cls.separator)),
            "reference_open": str(reference.get("open", cls.reference_open)),
            "reference_close": str(reference.get("close", cls.reference_close)),
            "reference_marker": str(reference.get("marker", cls.reference_marker)),
        }
        allowed_numbering = {"arabic", "roman", "source"}
        if values["chapter_numbering"] not in allowed_numbering:
            raise ValueError("chapter.numbering must be arabic, roman, or source")
        if values["article_numbering"] not in allowed_numbering:
            raise ValueError("article.numbering must be arabic, roman, or source")
        if not values["separator"]:
            raise ValueError("numbering separator must not be empty")
        return cls(**values)


@dataclass(frozen=True, slots=True)
class NumberedSource:
    number_text: str
    ordinal: int
    kind: str
    body: str


@dataclass(frozen=True, slots=True)
class DocumentReference:
    prefix: str
    year: str
    serial: str
    marker: str
    opening: str
    closing: str


@dataclass(frozen=True, slots=True)
class LayoutContract:
    page_number: int
    role: str
    source_text: str
    bbox: tuple[float, float, float, float]
    line_count: int
    center_x: float
    alignment: int
    one_line_preferred: bool
    source_font_size: float
    source_color: int
    source_font: str
    source_block_index: int
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LayoutOperation:
    page_number: int
    candidate_block_index: int
    role: str
    text: str
    rect: tuple[float, float, float, float]
    fontsize: float
    fontfile: str | None
    fontname: str
    color: tuple[float, float, float]
    align: int
    one_line: bool
    candidate_block_indices: tuple[int, ...] = ()


class LayoutContractError(RuntimeError):
    """Raised when a deterministic layout contract cannot be satisfied."""


def default_numbering_profile(target_language: str) -> NumberingProfile:
    """Return a replaceable locale default, never a document-specific rule."""

    language = target_language.strip().casefold()
    if language in {"en", "en-us", "en-gb", "english"}:
        return NumberingProfile(target_language=target_language)
    # Structural re-rendering is only applied when a caller supplies a profile
    # for a non-English target.  The recogniser remains reusable for all files.
    return NumberingProfile(
        target_language=target_language,
        chapter_label="",
        article_label="",
        chapter_numbering="source",
        article_numbering="source",
    )


def load_numbering_profile(
    path: str | Path | None,
    *,
    target_language: str,
) -> NumberingProfile:
    if path is None:
        return default_numbering_profile(target_language)
    profile_path = Path(path)
    try:
        payload = json.loads(profile_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"PDF style profile not found: {profile_path}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(f"PDF style profile is not valid JSON: {profile_path}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("PDF style profile root must be a JSON object")
    return NumberingProfile.from_mapping(payload, target_language=target_language)


def chinese_numeral_to_int(value: str) -> int:
    """Convert common Chinese ordinals without assuming a maximum chapter."""

    compact = re.sub(r"\s+", "", value)
    if not compact:
        raise ValueError("empty Chinese numeral")
    if compact.isdigit():
        return int(compact)
    if any(char not in _CN_DIGITS and char not in _CN_SMALL_UNITS and char not in _CN_LARGE_UNITS for char in compact):
        raise ValueError(f"unsupported Chinese numeral: {value}")
    total = 0
    section = 0
    number = 0
    for char in compact:
        if char in _CN_DIGITS:
            number = _CN_DIGITS[char]
            continue
        if char in _CN_SMALL_UNITS:
            unit = _CN_SMALL_UNITS[char]
            section += (number or 1) * unit
            number = 0
            continue
        unit = _CN_LARGE_UNITS[char]
        section = (section + number) * unit
        total += section
        section = 0
        number = 0
    return total + section + number


def parse_numbered_source(text: str) -> NumberedSource | None:
    match = _NUMBERED_SOURCE_RE.search(text.replace("\r", ""))
    if not match:
        return None
    number_text = re.sub(r"\s+", "", match.group("number"))
    try:
        ordinal = chinese_numeral_to_int(number_text)
    except ValueError:
        return None
    kind_map = {"章": "chapter", "条": "article", "节": "section", "款": "clause", "项": "item"}
    return NumberedSource(
        number_text=number_text,
        ordinal=ordinal,
        kind=kind_map[match.group("kind")],
        body=_WHITESPACE_RE.sub(" ", match.group("body")).strip(),
    )


def parse_document_reference(text: str) -> DocumentReference | None:
    match = _DOCUMENT_REFERENCE_RE.search(text.replace("\r", "").replace("\n", " "))
    if not match:
        return None
    return DocumentReference(
        prefix=match.group("prefix"),
        year=match.group("year"),
        serial=match.group("serial"),
        marker=match.group("marker") or "",
        opening=match.group("open"),
        closing=match.group("close"),
    )


def _roman(value: int) -> str:
    if value <= 0:
        return str(value)
    pairs = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))
    result: list[str] = []
    remaining = value
    for unit, token in pairs:
        count, remaining = divmod(remaining, unit)
        result.append(token * count)
    return "".join(result)


def _format_number(numbered: NumberedSource, numbering: str) -> str:
    if numbering == "roman":
        return _roman(numbered.ordinal)
    if numbering == "source":
        return numbered.number_text
    return str(numbered.ordinal)


def _strip_target_label(text: str) -> str:
    """Remove a model-produced structural label, including ``Article 1Text``."""

    value = _SOFT_HYPHEN_RE.sub("", text).strip()
    # Providers occasionally echo the structural label more than once (for
    # example ``Article 1 Article 1 ...``).  Strip only consecutive recognised
    # label/ordinal pairs; arbitrary prose beginning with "Article" is left
    # untouched once the next token is not an ordinal.
    while True:
        match = _TARGET_LABEL_RE.match(value)
        if not match:
            return value
        rest = match.group("rest")
        value = rest.lstrip(" \t\r\n:：.,;；-—–)]}")


def normalize_numbered_translation(
    source_text: str,
    translated_text: str,
    profile: NumberingProfile,
) -> str:
    """Rebuild a chapter/article label while retaining model-translated body."""

    numbered = parse_numbered_source(source_text)
    if numbered is None:
        return translated_text
    if numbered.kind == "chapter":
        label = profile.chapter_label
        numbering = profile.chapter_numbering
    elif numbered.kind == "article":
        label = profile.article_label
        numbering = profile.article_numbering
    else:
        return translated_text
    if not label:
        return translated_text
    body = _strip_target_label(translated_text)
    formatted = f"{label}{profile.separator}{_format_number(numbered, numbering)}"
    return formatted if not body else f"{formatted}{profile.separator}{body}"


def normalize_document_reference_translation(
    source_text: str,
    translated_text: str,
    profile: NumberingProfile,
) -> str:
    """Rebuild only numeric reference fields; issuer text remains translated.

    This never invents an issuer translation.  If a provider leaves CJK issuer
    text in a Latin-target output, the caller must fail validation and request a
    new translation or a glossary entry.
    """

    source_reference = parse_document_reference(source_text)
    if source_reference is None or not profile.reference_marker:
        return translated_text
    target = _SOFT_HYPHEN_RE.sub("", translated_text).replace("\n", " ")
    number_pattern = re.compile(
        rf"(?P<prefix>.*?)\s*[〔\[\(（]\s*{re.escape(source_reference.year)}\s*"
        rf"[〕\]\)）]\s*{re.escape(source_reference.serial)}\s*(?P<tail>.*)$",
        re.S,
    )
    match = number_pattern.search(target)
    if not match:
        return translated_text
    prefix = match.group("prefix").strip(" \t\r\n:：")
    if not prefix or _CJK_RE.search(prefix):
        # Do not silently transliterate or delete an unknown issuer.  The strict
        # language validator reports this and the gateway can retry/glossary it.
        return translated_text
    return (
        f"{prefix}{profile.separator}{profile.reference_open}{source_reference.year}"
        f"{profile.reference_close}{profile.separator}{profile.reference_marker}"
        f"{profile.separator}{source_reference.serial}"
    )


def _line_data(block: Mapping[str, Any]) -> list[tuple[str, tuple[float, float, float, float], float, int, str]]:
    raw: list[tuple[str, tuple[float, float, float, float], float, int, str]] = []
    for line in block.get("lines", []):
        spans = [span for span in line.get("spans", []) if str(span.get("text", "")).strip()]
        if not spans:
            continue
        boxes = [tuple(float(value) for value in span.get("bbox", (0, 0, 0, 0))) for span in spans]
        bbox = (min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes))
        sizes = [float(span.get("size", 0) or 0) for span in spans]
        colors = [int(span.get("color", 0) or 0) for span in spans]
        font = str(spans[0].get("font", ""))
        raw.append(("".join(str(span.get("text", "")) for span in spans), bbox, median(sizes) if sizes else 0.0, colors[0] if colors else 0, font))
    # PDF producers frequently emit one ``line`` object per horizontally
    # separated fragment while keeping an identical baseline.  Merge those
    # fragments into one visual line before inferring alignment or line count.
    result: list[tuple[str, tuple[float, float, float, float], float, int, str]] = []
    for item in sorted(raw, key=lambda value: (value[1][1], value[1][0])):
        if result and abs(item[1][1] - result[-1][1][1]) <= 1.5 and abs(item[1][3] - result[-1][1][3]) <= 2.5:
            previous = result[-1]
            result[-1] = (
                previous[0] + item[0],
                (min(previous[1][0], item[1][0]), min(previous[1][1], item[1][1]), max(previous[1][2], item[1][2]), max(previous[1][3], item[1][3])),
                median((previous[2], item[2])),
                previous[3],
                previous[4],
            )
        else:
            result.append(item)
    return result


def _block_text(block: Mapping[str, Any]) -> str:
    return "\n".join(item[0] for item in _line_data(block)).strip()


def _dominant_style(block: Mapping[str, Any]) -> tuple[float, int, str]:
    lines = _line_data(block)
    if not lines:
        return 10.0, 0, ""
    return median(item[2] for item in lines), lines[0][3], lines[0][4]


def _is_footer(block: Mapping[str, Any], page_height: float) -> bool:
    bbox = block.get("bbox", (0, 0, 0, 0))
    return float(bbox[1]) > page_height - 35


def _content_bounds(page: Any) -> tuple[float, float]:
    blocks = [block for block in page.get_text("dict").get("blocks", []) if block.get("type") == 0 and _block_text(block) and not _is_footer(block, page.rect.height)]
    if not blocks:
        return page.rect.width * 0.1, page.rect.width * 0.9
    return min(float(block["bbox"][0]) for block in blocks), max(float(block["bbox"][2]) for block in blocks)


def _alignment(page: Any, block: Mapping[str, Any], content_bounds: tuple[float, float], font_size: float) -> int:
    lines = _line_data(block)
    if not lines:
        return 0
    left, right = content_bounds
    center = (left + right) / 2
    width = max(right - left, 1.0)
    centers = [((line[1][0] + line[1][2]) / 2) for line in lines]
    tolerance = max(4.0, page.rect.width * 0.015)
    centered_lines = sum(abs(value - center) <= tolerance for value in centers)
    if centered_lines >= max(1, math.ceil(len(centers) * 0.8)):
        left_gaps = [line[1][0] - left for line in lines]
        right_gaps = [right - line[1][2] for line in lines]
        # A full-width paragraph often has a geometric centre close to the
        # page centre by accident. Treat it as centred only when both margins
        # are genuinely open, or when a large display face is being used.
        open_margins = (
            sum(gap > tolerance * 1.5 for gap in left_gaps) >= max(1, math.ceil(len(lines) * 0.8))
            and sum(gap > tolerance * 1.5 for gap in right_gaps) >= max(1, math.ceil(len(lines) * 0.8))
        )
        if open_margins or font_size >= 18:
            return 1
    if sum(line[1][0] - left <= tolerance for line in lines) >= max(1, math.ceil(len(lines) * 0.8)):
        return 0
    if (
        sum(right - line[1][2] <= tolerance for line in lines) >= max(1, math.ceil(len(lines) * 0.8))
        and sum(line[1][0] - left >= width * 0.25 for line in lines) >= max(1, math.ceil(len(lines) * 0.8))
    ):
        return 2
    return 0


def _role_for_source(text: str, alignment: int, font_size: float) -> tuple[str, dict[str, Any]]:
    numbered = parse_numbered_source(text)
    if numbered is not None:
        if numbered.kind == "chapter":
            return "chapter_heading", {"numbered": numbered}
        if numbered.kind == "article":
            # Long article paragraphs are not headings.  They still get a
            # deterministic Article label, but they keep normal paragraph flow.
            role = "article_heading" if "\n" not in text and len(numbered.body) <= 10 and "。" not in numbered.body else "article_text"
            return role, {"numbered": numbered}
    if parse_document_reference(text) is not None:
        return "document_reference", {}
    stripped = text.strip()
    if stripped.endswith(("：", ":")) and 6 <= len(stripped) <= 120:
        return "salutation", {}
    if alignment == 1 and font_size >= 14:
        return "centered_text", {}
    return "ordinary", {}


def build_layout_contracts(source_path: str | Path) -> tuple[LayoutContract, ...]:
    """Extract deterministic roles and source geometry without editing the PDF."""

    import fitz

    source = fitz.open(source_path)
    contracts: list[LayoutContract] = []
    try:
        for page_number, page in enumerate(source, 1):
            payload = page.get_text("dict")
            content_bounds = _content_bounds(page)
            raw: list[LayoutContract] = []
            for block_index, block in enumerate(payload.get("blocks", [])):
                if block.get("type") != 0 or not _block_text(block):
                    continue
                text = _block_text(block)
                font_size, color, font = _dominant_style(block)
                alignment = _alignment(page, block, content_bounds, font_size)
                role, metadata = _role_for_source(text, alignment, font_size)
                if role == "ordinary":
                    continue
                lines = _line_data(block)
                bbox = tuple(float(value) for value in block["bbox"])
                raw.append(
                    LayoutContract(
                        page_number=page_number,
                        role=role,
                        source_text=text,
                        bbox=bbox,
                        line_count=max(1, len(lines)),
                        center_x=(bbox[0] + bbox[2]) / 2,
                        alignment=alignment,
                        one_line_preferred=role in {"chapter_heading", "article_heading", "document_reference", "salutation"} or (role == "centered_text" and len(lines) == 1),
                        source_font_size=font_size,
                        source_color=color,
                        source_font=font,
                        source_block_index=block_index,
                        metadata={**metadata, "content_bounds": content_bounds},
                    )
                )
            # An article paragraph is frequently emitted as one PDF text
            # block per source line (the first line starts with ``第...条``
            # while continuation lines use the normal left margin).  Expand
            # that structural contract across the contiguous source blocks
            # before matching the translated PDF.  Otherwise only the first
            # line would be replaced and the remaining source-language or
            # provider-language continuation could be left orphaned on the
            # page.
            expanded: list[LayoutContract] = []
            raw_indices = [item.source_block_index for item in raw]
            payload_blocks = payload.get("blocks", [])
            for raw_index, current in enumerate(raw):
                if current.role != "article_text":
                    expanded.append(current)
                    continue
                next_boundary = raw_indices[raw_index + 1] if raw_index + 1 < len(raw_indices) else len(payload_blocks)
                continuation: list[tuple[int, Mapping[str, Any]]] = []
                for block_index in range(current.source_block_index + 1, next_boundary):
                    block = payload_blocks[block_index]
                    if block.get("type") == 0 and _block_text(block):
                        continuation.append((block_index, block))
                if not continuation:
                    expanded.append(current)
                    continue
                all_blocks = [payload_blocks[current.source_block_index], *(item[1] for item in continuation)]
                boxes = [tuple(float(value) for value in block["bbox"]) for block in all_blocks]
                expanded.append(
                    LayoutContract(
                        page_number=current.page_number,
                        role=current.role,
                        source_text="\n".join([current.source_text, *( _block_text(block) for _, block in continuation)]),
                        bbox=(min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes)),
                        line_count=sum(max(1, len(_line_data(block))) for block in all_blocks),
                        center_x=current.center_x,
                        alignment=current.alignment,
                        one_line_preferred=False,
                        source_font_size=current.source_font_size,
                        source_color=current.source_color,
                        source_font=current.source_font,
                        source_block_index=current.source_block_index,
                        metadata={**dict(current.metadata), "merged_block_indices": [current.source_block_index, *(item[0] for item in continuation)]},
                    )
                )
            raw = expanded
            # Co-linear title fragments are a single visual contract.  The
            # grouping is geometric and only applies to adjacent contracts with
            # the same role, so columns and table cells are not merged.
            index = 0
            while index < len(raw):
                current = raw[index]
                group = [current]
                index += 1
                while index < len(raw):
                    candidate = raw[index]
                    gap = candidate.bbox[1] - group[-1].bbox[3]
                    same_band = abs(candidate.center_x - current.center_x) <= page.rect.width * 0.08
                    if candidate.role != current.role or gap > 8 or not same_band:
                        break
                    group.append(candidate)
                    index += 1
                if len(group) == 1:
                    contracts.append(current)
                    continue
                contracts.append(
                    LayoutContract(
                        page_number=page_number,
                        role=current.role,
                        source_text="\n".join(item.source_text for item in group),
                        bbox=(min(item.bbox[0] for item in group), min(item.bbox[1] for item in group), max(item.bbox[2] for item in group), max(item.bbox[3] for item in group)),
                        line_count=sum(item.line_count for item in group),
                        center_x=sum(item.center_x for item in group) / len(group),
                        alignment=current.alignment,
                        one_line_preferred=len(group) == 1 and all(item.one_line_preferred for item in group),
                        source_font_size=median(item.source_font_size for item in group),
                        source_color=current.source_color,
                        source_font=current.source_font,
                        source_block_index=current.source_block_index,
                        metadata={**dict(current.metadata), "merged_block_indices": [item.source_block_index for item in group]},
                    )
                )
    finally:
        source.close()
    return tuple(contracts)


def _font_name_key(value: object) -> str:
    """Normalise a PDF/system font family name for collision checks."""

    text = str(value or "").casefold().split("+")[-1]
    if text.endswith((".ttf", ".otf", ".ttc")):
        text = text.rsplit(".", 1)[0]
    return re.sub(r"[^a-z0-9]+", "", text)


def _font_file(
    font_name: str,
    text: str,
    *,
    page: Any | None = None,
    prefer_narrow: bool = False,
) -> str | None:
    """Choose a target font without creating same-family PDF CMap collisions.

    PyMuPDF keys Type0 font resources by the embedded BaseFont family.  Adding
    a second file with the same family (for example, Noto Serif) can make the
    existing BabelDOC text layer decode with the overlay font's CMap.  Prefer a
    different installed family for an overlay and fall back to a built-in
    font at the call site when every candidate family is already present.
    """

    name = font_name.casefold()
    if _CJK_RE.search(text):
        candidates = (
            r"C:\Windows\Fonts\Noto Sans SC (TrueType).otf",
            r"C:\Windows\Fonts\simhei.ttf",
            r"C:\Windows\Fonts\msyh.ttc",
        )
    elif "times" in name or ("noto" in name and "serif" in name) or "serif" in name:
        # Times New Roman is a stable serif fallback whose family is normally
        # absent from BabelDOC's CJK/serif asset set.
        candidates = (
            r"C:\Windows\Fonts\ARIALN.TTF",
            r"C:\Windows\Fonts\times.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        ) if prefer_narrow else (
            r"C:\Windows\Fonts\times.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        )
    elif "arial" in name:
        candidates = (
            r"C:\Windows\Fonts\ARIALN.TTF",
            r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\times.ttf",
        ) if prefer_narrow else (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\times.ttf")
    elif "fang" in name:
        candidates = (
            r"C:\Windows\Fonts\simfang.ttf",
            r"C:\Windows\Fonts\arial.ttf",
        )
    else:
        candidates = (
            r"C:\Windows\Fonts\ARIALN.TTF",
            r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\times.ttf",
        ) if prefer_narrow else (
            r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\times.ttf",
        )

    existing_families: set[str] = set()
    if page is not None:
        try:
            for resource in page.get_fonts(full=True):
                if len(resource) > 3:
                    existing_families.add(_font_name_key(resource[3]))
        except Exception:
            # Font inspection is advisory.  The PDF writer/validator remains
            # responsible for rejecting a malformed candidate.
            existing_families = set()

    import fitz

    for candidate in candidates:
        path = Path(candidate)
        if not path.is_file():
            continue
        try:
            family = _font_name_key(fitz.Font(fontfile=candidate).name)
        except Exception:
            family = _font_name_key(path.stem)
        if family not in existing_families:
            return candidate
    return None


def _rgb(color: int) -> tuple[float, float, float]:
    value = int(color) & 0xFFFFFF
    return ((value >> 16) / 255.0, ((value >> 8) & 0xFF) / 255.0, (value & 0xFF) / 255.0)


def _candidate_text_blocks(page: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for block_index, block in enumerate(page.get_text("dict").get("blocks", [])):
        if block.get("type") != 0 or not _block_text(block) or _is_footer(block, page.rect.height):
            continue
        font_size, color, font = _dominant_style(block)
        result.append({"index": block_index, "indices": (block_index,), "text": _block_text(block), "bbox": tuple(float(value) for value in block["bbox"]), "font_size": font_size, "color": color, "font": font, "lines": _line_data(block), "block": block})
    return result


def _merge_reflow_candidate_blocks(
    contract: LayoutContract,
    start: dict[str, Any],
    blocks: list[dict[str, Any]],
    used: set[int],
) -> dict[str, Any]:
    """Collect visual candidate blocks belonging to one semantic contract.

    BabelDOC may retain separate text blocks for source continuation lines.
    The source contract supplies a bounded vertical region, while a new
    structural label terminates the group.  This is intentionally geometric
    and label-based; it does not depend on any document's wording.
    """

    ordered = sorted(blocks, key=lambda item: (item["bbox"][1], item["bbox"][0], item["index"]))
    start_position = next((position for position, item in enumerate(ordered) if item["index"] == start["index"]), None)
    if start_position is None:
        return start
    selected = [start]
    source_bottom = contract.bbox[3]
    # A one-line centered title commonly sits immediately above a table
    # header.  Do not absorb that next row merely because it is close in Y;
    # multiline display titles still get the wider continuation tolerance.
    max_top = source_bottom + (
        4.0 if contract.role == "centered_text" and contract.one_line_preferred
        else max(8.0, contract.source_font_size * 0.75)
    )
    if contract.role == "centered_text":
        # A centered source block is often split into two candidate blocks;
        # nearest-centre matching may land on the second line. Pull the
        # preceding visual line into the same semantic block so the original
        # first line is redacted instead of being left underneath.
        for item in reversed(ordered[:start_position]):
            if item["index"] in used:
                continue
            if item["bbox"][3] < contract.bbox[1] - max(8.0, contract.source_font_size):
                break
            if item["bbox"][0] < contract.bbox[2] and item["bbox"][2] > contract.bbox[0]:
                selected.insert(0, item)
    for item in ordered[start_position + 1 :]:
        if item["index"] in used:
            continue
        if item["bbox"][1] > max_top:
            break
        if _TARGET_LABEL_RE.match(_candidate_structure_text(item["text"] or "")):
            break
        if item["bbox"][2] <= contract.bbox[0] or item["bbox"][0] >= contract.bbox[2]:
            continue
        selected.append(item)
    if len(selected) == 1:
        return start
    boxes = [item["bbox"] for item in selected]
    lines = [line for item in selected for line in item["lines"]]
    return {
        **start,
        "indices": tuple(item["index"] for item in selected),
        # Candidate blocks are visual lines, not semantic paragraphs.  Keep
        # them in one reflowable text run so the textbox can use the available
        # width instead of reproducing BabelDOC's premature hard breaks.
        "text": " ".join(item["text"] for item in selected),
        "bbox": (min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes)),
        "font_size": median(item["font_size"] for item in selected),
        "color": selected[0]["color"],
        "font": selected[0]["font"],
        "lines": lines,
        "block": selected[0]["block"],
    }
def _choose_candidate_block(contract: LayoutContract, blocks: list[dict[str, Any]], used: set[int]) -> dict[str, Any] | None:
    source_y = (contract.bbox[1] + contract.bbox[3]) / 2
    scored: list[tuple[float, dict[str, Any]]] = []
    for block in blocks:
        if block["index"] in used:
            continue
        bbox = block["bbox"]
        candidate_y = (bbox[1] + bbox[3]) / 2
        y_distance = abs(candidate_y - source_y)
        overlap = max(0.0, min(contract.bbox[3], bbox[3]) - max(contract.bbox[1], bbox[1]))
        # Display blocks and structural headings normally retain their source
        # vertical anchor through translation.  A distant block is therefore
        # not a valid fallback: choosing it would silently paint a title over
        # a table header or paragraph when the provider omitted the title.
        # Ordinary article text is allowed a wider search window because its
        # translated line count can legitimately move neighbouring content.
        if contract.role in {"centered_text", "document_reference", "salutation", "chapter_heading", "article_heading"}:
            max_y_distance = max(12.0, contract.bbox[3] - contract.bbox[1] + 4.0)
        else:
            max_y_distance = max(45.0, contract.bbox[3] - contract.bbox[1] + 20.0)
        if y_distance > max_y_distance and overlap <= 0:
            continue
        score = y_distance - min(overlap, 25.0) * 0.35
        if contract.role in {"chapter_heading", "article_heading", "article_text"} and not re.match(r"^\s*(chapter|part|section|article|clause|item)\b", _candidate_structure_text(block["text"]), re.I):
            score += 18.0
        scored.append((score, block))
    if not scored:
        return None
    selected = min(scored, key=lambda item: item[0])[1]
    if contract.role in {"article_text", "centered_text"}:
        return _merge_reflow_candidate_blocks(contract, selected, blocks, used)
    return selected


def _fit_rect(contract: LayoutContract, page: Any, blocks: list[dict[str, Any]], candidate: dict[str, Any]) -> tuple[float, float, float, float]:
    source_bounds = contract.metadata.get("content_bounds") if isinstance(contract.metadata, Mapping) else None
    if (
        isinstance(source_bounds, (tuple, list))
        and len(source_bounds) == 2
        and all(isinstance(value, (int, float)) for value in source_bounds)
    ):
        left, right = float(source_bounds[0]), float(source_bounds[1])
    else:
        left, right = _content_bounds(page)
    source = contract.bbox
    next_y = page.rect.height - 18.0
    for block in blocks:
        if block["bbox"][1] > source[3] + 0.5 and block["bbox"][0] < right and block["bbox"][2] > left:
            next_y = min(next_y, block["bbox"][1] - 2.0)
    y0 = max(0.0, source[1] - 1.0)
    y1 = max(source[3] + 2.0, next_y)
    if contract.one_line_preferred:
        # ``insert_textbox`` can return a negative value when the box is only
        # as high as the source glyph bbox (font ascender/descender metrics
        # need a little extra leading).  Reserve a small, role-independent
        # line box so a valid single-line title is not silently dropped.
        y1 = max(y1, y0 + max(16.0, contract.source_font_size * 1.5))
    if contract.alignment == 1:
        # Centered display blocks still obey the source page's content bounds;
        # centering is performed inside the preserved margins, not across the
        # physical trim box.
        return (left, y0, right, y1)
    # Keep the original left anchor while allowing the English expansion to use
    # the unused right side.  This is what avoids needless wraps in salutations
    # and article labels without centring ordinary paragraphs.
    return (max(0.0, source[0] - 1.0), y0, right, y1)


def _text_width(text: str, fontsize: float, fontfile: str | None) -> float:
    import fitz

    if fontfile:
        return float(fitz.Font(fontfile=fontfile).text_length(text, fontsize=fontsize))
    return float(fitz.get_text_length(text, fontname="helv", fontsize=fontsize))


def _probe_line_count(
    text: str,
    rect: tuple[float, float, float, float],
    *,
    fontsize: float,
    fontfile: str | None,
    fontname: str,
    align: int,
) -> tuple[bool, int]:
    """Probe fit and line count on an isolated page without touching output."""

    import fitz

    probe = fitz.open()
    try:
        test_page = probe.new_page(width=max(rect[2], 1.0), height=max(rect[3], 1.0))
        result = test_page.insert_textbox(
            fitz.Rect(rect),
            text,
            fontsize=fontsize,
            fontfile=fontfile,
            fontname=fontname,
            align=align,
        )
        if result < 0:
            return False, 0
        blocks = [
            block
            for block in test_page.get_text("dict").get("blocks", [])
            if block.get("type") == 0
        ]
        line_count = sum(len(block.get("lines", [])) for block in blocks)
        return True, max(1, line_count)
    finally:
        probe.close()


def _fit_fontsize(
    text: str,
    rect: tuple[float, float, float, float],
    base: float,
    minimum: float,
    fontfile: str | None,
    fontname: str,
    align: int,
    one_line: bool,
    preferred_line_count: int | None = None,
) -> float:
    available = max(1.0, rect[2] - rect[0] - 4.0)
    size = max(minimum, base)
    while size >= minimum - 1e-6:
        if one_line:
            if _text_width(text.replace("\n", " "), size, fontfile) > available:
                size = round(size - 0.5, 2)
                continue
            fits, line_count = _probe_line_count(
                text.replace("\n", " "),
                rect,
                fontsize=size,
                fontfile=fontfile,
                fontname=fontname,
                align=align,
            )
            if not fits or line_count > 1:
                size = round(size - 0.5, 2)
                continue
            return round(size, 2)
        if not one_line:
            fits, line_count = _probe_line_count(
                text,
                rect,
                fontsize=size,
                fontfile=fontfile,
                fontname=fontname,
                align=align,
            )
            if not fits or (
                preferred_line_count is not None
                and line_count > preferred_line_count
            ):
                size = round(size - 0.5, 2)
                continue
        if fits:
            return round(size, 2)
    raise LayoutContractError(f"layout contract cannot fit one line at minimum font size {minimum:g}: {text[:80]}")


def _normalise_render_text(text: str, *, one_line: bool) -> str:
    # Some BabelDOC fonts expose an unmapped space glyph as U+0000.  Never
    # feed that control byte back into a replacement textbox: PyMuPDF would
    # paint .notdef squares and reintroduce the invalid character into the
    # validated output.  Newlines remain semantic line boundaries.
    text = _UNSAFE_TEXT_CONTROL_RE.sub(" ", text)
    text = _SOFT_HYPHEN_RE.sub("", text)
    if one_line:
        return _WHITESPACE_RE.sub(" ", text).strip()
    # BabelDOC emits one block per visual line in many translated PDFs.  For
    # article prose those newlines are not semantic paragraph boundaries and
    # retaining them needlessly wastes the right side of the source textbox.
    # Structural headings and genuinely multiline display text keep their
    # explicit lines.
    return "\n".join(_WHITESPACE_RE.sub(" ", line).strip() for line in text.splitlines()).strip()


def _candidate_structure_text(text: str) -> str:
    """Make provider control-byte space surrogates visible to role parsing."""

    return _WHITESPACE_RE.sub(" ", _UNSAFE_TEXT_CONTROL_RE.sub(" ", text)).strip()


def _operation_for(
    contract: LayoutContract,
    candidate: dict[str, Any],
    page: Any,
    blocks: list[dict[str, Any]],
    *,
    profile: NumberingProfile,
    target_language: str,
    minimum_font_size: float,
) -> LayoutOperation:
    text = _UNSAFE_TEXT_CONTROL_RE.sub(" ", candidate["text"])
    if contract.role in {"chapter_heading", "article_heading", "article_text"}:
        text = normalize_numbered_translation(contract.source_text, text, profile)
    elif contract.role == "document_reference" and target_language.strip().casefold() in {"en", "en-us", "en-gb", "english"}:
        text = normalize_document_reference_translation(contract.source_text, text, profile)
    one_line = contract.one_line_preferred
    # Centered display blocks are reflowed from their semantic text rather
    # than inheriting provider line breaks.  This lets the fit probe preserve
    # the source block's line budget across fonts and document lengths.
    if contract.role in {"centered_text", "article_text"} and not one_line:
        text = _WHITESPACE_RE.sub(" ", _SOFT_HYPHEN_RE.sub("", text)).strip()
    else:
        text = _normalise_render_text(text, one_line=one_line)
    rect = _fit_rect(contract, page, blocks, candidate)
    base = contract.source_font_size if contract.role in {"centered_text", "chapter_heading", "article_heading", "article_text", "salutation", "document_reference"} else candidate["font_size"]
    # English prose should try the policy's narrow face before reducing the
    # point size.  This is especially important for translated table/contract
    # paragraphs whose source line budget still has usable vertical space.
    fontfile = _font_file(candidate["font"], text, page=page, prefer_narrow=contract.role == "article_text")
    fontname = (
        "PDFLayout_" + re.sub(r"[^A-Za-z0-9]", "", Path(fontfile).stem)[:20]
        if fontfile
        else ("tiro" if "serif" in candidate["font"].casefold() else "helv")
    )
    align = contract.alignment
    preferred_line_count = (
        contract.line_count
        if contract.role == "centered_text" and not one_line
        else None
    )
    fontsize = _fit_fontsize(
        text,
        rect,
        base,
        minimum_font_size,
        fontfile,
        fontname,
        align,
        one_line,
        preferred_line_count,
    )
    # Preserve candidate style where available; source color is used for source
    # title contracts because BabelDOC may have split its spans.
    color = _rgb(candidate["color"] if contract.role != "centered_text" else contract.source_color)
    return LayoutOperation(contract.page_number, candidate["index"], contract.role, text, rect, fontsize, fontfile, fontname, color, align, one_line, tuple(candidate.get("indices", (candidate["index"],))))


def restore_layout_contract(
    source_path: str | Path,
    candidate_path: str | Path,
    destination_path: str | Path,
    *,
    target_language: str,
    profile: NumberingProfile | None = None,
    minimum_font_size: float = 6.0,
) -> dict[str, Any]:
    """Restore generic special-block contracts in a candidate PDF.

    Only text spans belonging to recognised special blocks are redacted.  PDF
    images and vector drawings are explicitly retained.  The function writes a
    new candidate and returns an audit summary; it never overwrites the source.
    """

    import fitz

    profile = profile or default_numbering_profile(target_language)
    contracts = build_layout_contracts(source_path)
    source = fitz.open(source_path)
    candidate = fitz.open(candidate_path)
    operations: list[LayoutOperation] = []
    unmatched: list[dict[str, Any]] = []
    try:
        for page_number in range(1, min(source.page_count, candidate.page_count) + 1):
            page = candidate[page_number - 1]
            blocks = _candidate_text_blocks(page)
            used: set[int] = set()
            page_contracts = [item for item in contracts if item.page_number == page_number]
            for contract in page_contracts:
                selected = _choose_candidate_block(contract, blocks, used)
                if selected is None:
                    unmatched.append({"page": page_number, "role": contract.role, "source_text": contract.source_text})
                    continue
                try:
                    operation = _operation_for(contract, selected, page, blocks, profile=profile, target_language=target_language, minimum_font_size=minimum_font_size)
                except LayoutContractError:
                    raise
                operations.append(operation)
                used.update(selected.get("indices", (selected["index"],)))
        if unmatched:
            # Ordinary documents may not have every role in the candidate when
            # a provider omitted a block.  Failing closed is safer than drawing
            # a guessed replacement over an unrelated paragraph.
            raise LayoutContractError("unmatched PDF layout contracts: " + json.dumps(unmatched, ensure_ascii=False))
        by_page: dict[int, list[LayoutOperation]] = {}
        for operation in operations:
            by_page.setdefault(operation.page_number, []).append(operation)
        for page_number, page_operations in by_page.items():
            page = candidate[page_number - 1]
            block_by_index = {block["index"]: block for block in _candidate_text_blocks(page)}
            for operation in page_operations:
                block_indices = operation.candidate_block_indices or (operation.candidate_block_index,)
                for block_index in block_indices:
                    block = block_by_index.get(block_index)
                    if block is not None:
                        page.add_redact_annot(fitz.Rect(block["bbox"]), fill=(1, 1, 1), cross_out=False)
            # Keep images and vector drawings; only existing text is removed.
            page.apply_redactions(images=0, graphics=0, text=0)
            for operation in page_operations:
                page.insert_textbox(
                    fitz.Rect(operation.rect),
                    operation.text,
                    fontsize=operation.fontsize,
                    fontfile=operation.fontfile,
                    fontname=operation.fontname,
                    color=operation.color,
                    align=operation.align,
                    overlay=True,
                )
        candidate.save(destination_path)
    finally:
        candidate.close()
        source.close()
    return {
        "status": "restored",
        "contract_count": len(contracts),
        "operation_count": len(operations),
        "roles": {role: sum(1 for item in operations if item.role == role) for role in sorted({item.role for item in operations})},
        "profile": {
            "target_language": profile.target_language,
            "chapter_label": profile.chapter_label,
            "article_label": profile.article_label,
            "chapter_numbering": profile.chapter_numbering,
            "article_numbering": profile.article_numbering,
        },
    }


def validate_layout_contract(
    source_path: str | Path,
    candidate_path: str | Path,
    *,
    target_language: str,
    profile: NumberingProfile | None = None,
    minimum_font_size: float = 6.0,
) -> dict[str, Any]:
    """Compare recognised roles and source anchors with a candidate PDF."""

    import fitz

    profile = profile or default_numbering_profile(target_language)
    contracts = build_layout_contracts(source_path)
    candidate = fitz.open(candidate_path)
    failures: list[str] = []
    observed: list[dict[str, Any]] = []
    try:
        used_by_page: dict[int, set[int]] = {}
        for contract in contracts:
            if contract.page_number > candidate.page_count:
                failures.append(f"page {contract.page_number}: missing page")
                continue
            page = candidate[contract.page_number - 1]
            blocks = _candidate_text_blocks(page)
            used = used_by_page.setdefault(contract.page_number, set())
            selected = _choose_candidate_block(contract, blocks, used)
            if selected is None:
                failures.append(f"page {contract.page_number}: missing {contract.role}")
                continue
            used.update(selected.get("indices", (selected["index"],)))
            bbox = selected["bbox"]
            center = (bbox[0] + bbox[2]) / 2
            alignment_error = abs(center - contract.center_x)
            if contract.alignment == 1 and alignment_error > max(8.0, page.rect.width * 0.025):
                failures.append(f"page {contract.page_number}: {contract.role} center drift {alignment_error:.2f}pt")
            if selected["font_size"] + 1e-6 < minimum_font_size:
                failures.append(f"page {contract.page_number}: {contract.role} below minimum font size")
            if contract.one_line_preferred and len(selected["lines"]) != 1:
                failures.append(f"page {contract.page_number}: {contract.role} wrapped unexpectedly")
            if contract.role in {"chapter_heading", "article_heading", "article_text"}:
                numbered = parse_numbered_source(contract.source_text)
                if numbered is not None:
                    if numbered.kind == "chapter":
                        label = profile.chapter_label
                        numbering = profile.chapter_numbering
                    else:
                        label = profile.article_label
                        numbering = profile.article_numbering
                    if label:
                        expected_prefix = f"{label}{profile.separator}{_format_number(numbered, numbering)}"
                        observed_prefix = _WHITESPACE_RE.sub(" ", selected["text"]).lstrip()
                        expected_prefix = _WHITESPACE_RE.sub(" ", expected_prefix).lstrip()
                        if not observed_prefix.casefold().startswith(expected_prefix.casefold()):
                            failures.append(f"page {contract.page_number}: {contract.role} numbering or separator mismatch")
            observed.append({"page": contract.page_number, "role": contract.role, "source_block": contract.source_block_index, "candidate_block": selected["index"], "candidate_line_count": len(selected["lines"]), "center_error": round(alignment_error, 3), "text": selected["text"]})
    finally:
        candidate.close()
    return {"status": "passed" if not failures else "failed", "contract_count": len(contracts), "failures": failures, "observed": observed}


__all__ = [
    "DocumentReference",
    "LayoutContract",
    "LayoutContractError",
    "NumberedSource",
    "NumberingProfile",
    "build_layout_contracts",
    "chinese_numeral_to_int",
    "default_numbering_profile",
    "load_numbering_profile",
    "normalize_document_reference_translation",
    "normalize_numbered_translation",
    "parse_document_reference",
    "parse_numbered_source",
    "restore_layout_contract",
    "validate_layout_contract",
]
