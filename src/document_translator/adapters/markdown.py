"""Markdown extraction and conservative, source-span based rewrite."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

import marko

from document_translator.core import (
    DocumentFormat, DocumentLocation, TranslationResult, TranslationUnit,
    generate_unit_id, sha256_text, validate_placeholders, validate_result_for_unit,
)

_PLACEHOLDER_RE = re.compile(r"⟦[^⟧]+⟧")
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>|<!--[\s\S]*?-->")
_INLINE_CODE_RE = re.compile(r"(?<!\\)(`+)(.+?)(?<!`)\1")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]*)\)")
_LINK_RE = re.compile(r"(?<!\!)\[([^\]]+)\]\(([^)]*)\)")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
_FRONT_MATTER_MARKERS = {"---", "..."}


@dataclass(frozen=True)
class MarkdownReadResult:
    text: str
    encoding: str


@dataclass(frozen=True)
class MarkdownRewriteResult:
    text: str
    errors: tuple[str, ...]
    replaced_unit_ids: tuple[str, ...]


def _protected_tokens(text: str) -> list[str]:
    matches = list(_PLACEHOLDER_RE.finditer(text))
    matches.extend(_HTML_TAG_RE.finditer(text))
    matches.extend(re.finditer(r"\\.", text))
    return list(dict.fromkeys(match.group(0) for match in sorted(matches, key=lambda item: item.start())))


def _trimmed_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start == end or not any(char.isalnum() for char in text[start:end]):
        return None
    return start, end


def _editable_spans(content: str, base: int) -> list[tuple[int, int]]:
    protected_ranges: list[tuple[int, int]] = []
    for pattern in (_INLINE_CODE_RE, _IMAGE_RE, _LINK_RE):
        protected_ranges.extend((match.start(), match.end()) for match in pattern.finditer(content))
    protected_ranges.sort()
    spans: list[tuple[int, int]] = []
    cursor = 0
    for start, end in protected_ranges:
        if start < cursor:
            continue
        if pattern_is_link := _IMAGE_RE.fullmatch(content[start:end]):
            alt_start = start + 2
            alt_end = alt_start + len(pattern_is_link.group(1))
            if _trimmed_span(content, alt_start, alt_end):
                spans.append((base + alt_start, base + alt_end))
        elif link_match := _LINK_RE.fullmatch(content[start:end]):
            label_start = start + 1
            label_end = label_start + len(link_match.group(1))
            if _trimmed_span(content, label_start, label_end):
                spans.append((base + label_start, base + label_end))
        if start > cursor:
            trimmed = _trimmed_span(content, cursor, start)
            if trimmed:
                spans.append((base + trimmed[0], base + trimmed[1]))
        cursor = end
    if cursor < len(content):
        trimmed = _trimmed_span(content, cursor, len(content))
        if trimmed:
            spans.append((base + trimmed[0], base + trimmed[1]))
    return sorted(spans)


def _line_content_span(line: str, line_start: int) -> tuple[int, int] | None:
    match = re.match(r"^\s*(?:#{1,6}\s+|(?:[-+*]|\d+[.)])\s+|>\s*)", line)
    start = match.end() if match else 0
    end = len(line.rstrip("\r\n"))
    return (line_start + start, line_start + end) if start < end else None


def extract_translation_units(
    text: str, *, source_language: str = "auto", target_language: str = "en",
    document_hash: str | None = None,
) -> list[TranslationUnit]:
    """Parse Markdown and return stable units for natural-language source spans."""
    marko.parse(text)
    document_hash = document_hash or sha256_text(text)
    units: list[TranslationUnit] = []
    offset = 0
    in_front_matter = False
    front_matter_seen = False
    fence: tuple[str, int] | None = None
    lines = text.splitlines(keepends=True)
    for line_number, line in enumerate(lines, start=1):
        bare = line.rstrip("\r\n")
        stripped = bare.strip()
        if line_number == 1 and stripped == "---":
            in_front_matter = True
            front_matter_seen = True
            offset += len(line)
            continue
        if in_front_matter:
            if stripped in _FRONT_MATTER_MARKERS:
                in_front_matter = False
            offset += len(line)
            continue
        fence_match = _FENCE_RE.match(bare)
        if fence:
            if fence_match and fence_match.group(1)[0] == fence[0] and len(fence_match.group(1)) >= fence[1]:
                fence = None
            offset += len(line)
            continue
        if fence_match:
            fence = (fence_match.group(1)[0], len(fence_match.group(1)))
            offset += len(line)
            continue
        content_span = _line_content_span(line, offset)
        if content_span:
            content_start, content_end = content_span
            content = text[content_start:content_end]
            for start, end in _editable_spans(content, content_start):
                location = DocumentLocation(
                    part="markdown",
                    object_id=f"line:{line_number}:span:{start - offset}:{end - offset}",
                    node_ids=[f"line:{line_number}", f"span:{start}:{end}"],
                )
                source = text[start:end]
                data = dict(
                    document_hash=document_hash, format=DocumentFormat.MD, location=location,
                    source_language=source_language, target_language=target_language,
                    source_text=source, protected_tokens=_protected_tokens(source),
                    style_signature="", context_before="", context_after="",
                )
                data["id"] = generate_unit_id(**data)
                units.append(TranslationUnit.model_validate(data))
        offset += len(line)
    return units


def rewrite_markdown(
    text: str, units: Sequence[TranslationUnit],
    translations: Mapping[str, str | TranslationResult],
) -> MarkdownRewriteResult:
    """Rewrite only supplied, valid source spans; all rejected items remain unchanged."""
    errors: list[str] = []
    replacements: list[tuple[int, int, str, str]] = []
    seen_spans: set[tuple[int, int]] = set()
    for unit in units:
        location = unit.location
        match = re.fullmatch(r"line:(\d+):span:(\d+):(\d+)", location.object_id or "")
        if not match:
            errors.append(f"{unit.id}: INVALID_LOCATION")
            continue
        line_number, start_in_line, end_in_line = map(int, match.groups())
        line_offsets = [0]
        for line in text.splitlines(keepends=True):
            line_offsets.append(line_offsets[-1] + len(line))
        if line_number >= len(line_offsets):
            errors.append(f"{unit.id}: INVALID_LOCATION")
            continue
        start = line_offsets[line_number - 1] + start_in_line
        end = line_offsets[line_number - 1] + end_in_line
        if text[start:end] != unit.source_text:
            errors.append(f"{unit.id}: SOURCE_SPAN_MISMATCH")
            continue
        if unit.id not in translations:
            errors.append(f"{unit.id}: MISSING_TRANSLATION")
            continue
        candidate = translations[unit.id]
        if candidate is None or (isinstance(candidate, str) and not candidate.strip()):
            errors.append(f"{unit.id}: MISSING_TRANSLATION")
            continue
        if isinstance(candidate, TranslationResult):
            if candidate.unit_id != unit.id:
                errors.append(f"{unit.id}: UNIT_ID_MISMATCH")
                continue
            item_errors = validate_result_for_unit(unit, candidate)
            candidate_text = candidate.translation
        else:
            item_errors = validate_placeholders(unit.source_text, candidate, unit.protected_tokens)
            candidate_text = candidate
        if item_errors:
            errors.extend(f"{unit.id}: {error}" for error in item_errors)
            continue
        if (start, end) in seen_spans:
            errors.append(f"{unit.id}: DUPLICATE_LOCATION")
            continue
        seen_spans.add((start, end))
        replacements.append((start, end, candidate_text, unit.id))
    result = text
    for start, end, replacement, unit_id in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    return MarkdownRewriteResult(result, tuple(errors), tuple(unit_id for *_, unit_id in replacements))


def read_markdown(path: str | Path) -> MarkdownReadResult:
    raw = Path(path).read_bytes()
    encoding = "utf-8-sig" if raw.startswith(b"\xef\xbb\xbf") else "utf-8"
    return MarkdownReadResult(raw.decode(encoding), encoding)


def write_markdown(path: str | Path, text: str, *, encoding: str = "utf-8", overwrite: bool = False) -> None:
    destination = Path(path)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing file: {destination}")
    destination.write_bytes(text.encode(encoding))
