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
_URL_RE = re.compile(r"https?://[^\s<>()]+")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})(.*)$")
_FRONT_MATTER_MARKERS = {"---", "..."}
_TABLE_ALIGNMENT_CELL_RE = re.compile(r"^:?-{3,}:?$")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_SENTENCE_END_RE = re.compile(r"[。！？；：.!?;:][”’）】〕〉》]?$")


def detect_hard_wraps(text: str) -> tuple[str, ...]:
    """Report likely CJK word splits before translation; do not silently hide them."""
    lines = text.splitlines()
    issues: list[str] = []
    for index, (left, right) in enumerate(zip(lines, lines[2:]), start=1):
        left = left.strip()
        right = right.strip()
        if (
            len(left) >= 60 and left and right and not _SENTENCE_END_RE.search(left)
            and _CJK_RE.fullmatch(left[-1]) and _CJK_RE.fullmatch(right[0])
        ):
            issues.append(f"lines {index} and {index + 2}: possible split word {left[-8:]} / {right[:8]}")
    return tuple(issues)


@dataclass(frozen=True)
class MarkdownReadResult:
    text: str
    encoding: str


@dataclass(frozen=True)
class MarkdownRewriteResult:
    text: str
    errors: tuple[str, ...]
    replaced_unit_ids: tuple[str, ...]


@dataclass(frozen=True)
class _ProtectedText:
    text: str
    tokens: tuple[str, ...]
    replacements: tuple[tuple[str, str], ...]


def _next_token(source: str, counter: int) -> tuple[str, int]:
    while True:
        token = f"⟦MD_{counter:04d}⟧"
        counter += 1
        if token not in source:
            return token, counter


def _protect_inline(source: str) -> _ProtectedText:
    """Replace non-translatable inline syntax with deterministic placeholders."""
    output: list[str] = []
    tokens: list[str] = []
    replacements: list[tuple[str, str]] = []
    counter = 1
    position = 0
    marker_ranges: dict[int, int] = {}
    marker_matches = list(re.finditer(r"\*{1,3}|_{1,3}|~~", source))
    marker_types = {match.group(0)[0] for match in marker_matches}
    for marker_type in marker_types:
        matches = [match for match in marker_matches if match.group(0)[0] == marker_type]
        if len(matches) < 2:
            continue
        for match in matches:
            if marker_type == "_":
                before = source[match.start() - 1] if match.start() else ""
                after = source[match.end()] if match.end() < len(source) else ""
                if before.isalnum() and after.isalnum():
                    continue
            marker_ranges[match.start()] = match.end()

    def protect(value: str) -> None:
        nonlocal counter
        token, counter = _next_token(source, counter)
        output.append(token)
        tokens.append(token)
        replacements.append((token, value))

    while position < len(source):
        placeholder = _PLACEHOLDER_RE.match(source, position)
        if placeholder:
            value = placeholder.group(0)
            output.append(value)
            if value not in tokens:
                tokens.append(value)
            position = placeholder.end()
            continue

        inline_code = _INLINE_CODE_RE.match(source, position)
        if inline_code:
            protect(inline_code.group(0))
            position = inline_code.end()
            continue

        image = _IMAGE_RE.match(source, position)
        if image:
            protect("![")
            output.append(image.group(1))
            protect(f"]({image.group(2)})")
            position = image.end()
            continue

        link = _LINK_RE.match(source, position)
        if link:
            protect("[")
            output.append(link.group(1))
            protect(f"]({link.group(2)})")
            position = link.end()
            continue

        html = _HTML_TAG_RE.match(source, position)
        if html:
            protect(html.group(0))
            position = html.end()
            continue

        url = _URL_RE.match(source, position)
        if url:
            protect(url.group(0))
            position = url.end()
            continue

        if source[position] == "\\" and position + 1 < len(source):
            protect(source[position:position + 2])
            position += 2
            continue

        if position in marker_ranges:
            end = marker_ranges[position]
            protect(source[position:end])
            position = end
            continue

        output.append(source[position])
        position += 1

    return _ProtectedText("".join(output), tuple(tokens), tuple(replacements))


def _trimmed_span(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start == end or not any(char.isalnum() for char in text[start:end]):
        return None
    return start, end


def _table_delimiters(line: str) -> list[int]:
    delimiters: list[int] = []
    code_ticks = 0
    link_depth = 0
    html = False
    position = 0
    while position < len(line):
        char = line[position]
        if char == "\\":
            position += 2
            continue
        if char == "`":
            end = position
            while end < len(line) and line[end] == "`":
                end += 1
            run = end - position
            code_ticks = 0 if code_ticks == run else run if code_ticks == 0 else code_ticks
            position = end
            continue
        if code_ticks:
            position += 1
            continue
        if char == "<":
            html = True
        elif char == ">" and html:
            html = False
        elif not html and char == "]" and position + 1 < len(line) and line[position + 1] == "(":
            link_depth = 1
            position += 2
            continue
        elif link_depth and char == "(":
            link_depth += 1
        elif link_depth and char == ")":
            link_depth -= 1
        elif char == "|" and not html and not link_depth:
            delimiters.append(position)
        position += 1
    return delimiters


def _table_cells(line: str, line_start: int, *, require_natural: bool = True) -> list[tuple[int, int]]:
    delimiters = _table_delimiters(line)
    if not delimiters:
        return []
    boundaries = [-1, *delimiters, len(line)]
    spans: list[tuple[int, int]] = []
    for left, right in zip(boundaries, boundaries[1:]):
        start, end = left + 1, right
        while start < end and line[start].isspace():
            start += 1
        while end > start and line[end - 1].isspace():
            end -= 1
        if start < end and (not require_natural or any(char.isalnum() for char in line[start:end])):
            spans.append((line_start + start, line_start + end))
    return spans


def _is_alignment_row(line: str) -> bool:
    cells = _table_cells(line, 0, require_natural=False)
    return bool(cells) and all(_TABLE_ALIGNMENT_CELL_RE.fullmatch(line[start:end]) for start, end in cells)


def _table_line_sets(lines: Sequence[str]) -> tuple[set[int], set[int]]:
    table_lines: set[int] = set()
    alignment_lines: set[int] = set()
    bare_lines = [line.rstrip("\r\n") for line in lines]
    for index in range(1, len(bare_lines)):
        if not _is_alignment_row(bare_lines[index]) or not _table_delimiters(bare_lines[index - 1]):
            continue
        table_lines.add(index)
        table_lines.add(index + 1)
        alignment_lines.add(index + 1)
        following = index + 1
        while following < len(bare_lines) and _table_delimiters(bare_lines[following]):
            table_lines.add(following + 1)
            following += 1
    return table_lines, alignment_lines


def _line_content_span(line: str, line_start: int) -> tuple[int, int] | None:
    match = re.match(r"^\s*(?:#{1,6}\s+|(?:[-+*]|\d+[.)])\s+|>\s*)", line)
    start = match.end() if match else 0
    end = len(line.rstrip("\r\n"))
    while end > start and line[end - 1] in " \t":
        end -= 1
    return (line_start + start, line_start + end) if start < end else None


def _merge_hard_wrapped_units(text: str, units: Sequence[TranslationUnit]) -> list[TranslationUnit]:
    """Merge a likely CJK word split across a blank line into one rewritable span."""
    merged: list[TranslationUnit] = []
    index = 0
    while index < len(units):
        left = units[index]
        right = units[index + 1] if index + 1 < len(units) else None
        left_match = re.fullmatch(r"line:(\d+):span:.*", left.location.object_id or "")
        right_match = re.fullmatch(r"line:(\d+):span:.*", right.location.object_id or "") if right else None
        should_merge = (
            right is not None
            and left_match is not None
            and right_match is not None
            and int(right_match.group(1)) == int(left_match.group(1)) + 2
            and len(left.source_text) >= 60
            and not left.protected_tokens
            and not right.protected_tokens
            and not _SENTENCE_END_RE.search(left.source_text)
            and _CJK_RE.fullmatch(left.source_text[-1]) is not None
            and _CJK_RE.fullmatch(right.source_text[0]) is not None
        )
        if not should_merge:
            merged.append(left)
            index += 1
            continue
        span_nodes = [node for node in (*left.location.node_ids, *right.location.node_ids) if node.startswith("span:")]
        start = int(span_nodes[0].split(":")[1])
        end = int(span_nodes[-1].split(":")[2])
        location = DocumentLocation(
            part="markdown", object_id=f"range:{start}:{end}", node_ids=span_nodes,
        )
        data = left.model_dump(exclude={"id", "status"})
        data.update(
            location=location,
            source_text=text[start:end],
            protected_tokens=[],
            context_before="",
            context_after="",
        )
        data["id"] = generate_unit_id(**data)
        merged.append(TranslationUnit.model_validate(data))
        index += 2
    return merged


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
    fence: tuple[str, int] | None = None
    lines = text.splitlines(keepends=True)
    table_lines, alignment_lines = _table_line_sets(lines)
    for line_number, line in enumerate(lines, start=1):
        bare = line.rstrip("\r\n")
        stripped = bare.strip()
        if line_number == 1 and stripped == "---":
            in_front_matter = True
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
        if line_number in alignment_lines:
            offset += len(line)
            continue
        if line_number in table_lines:
            spans = _table_cells(bare, offset)
        else:
            content_span = _line_content_span(line, offset)
            spans = [content_span] if content_span else []
        for start, end in spans:
                protected = _protect_inline(text[start:end])
                natural_text = _PLACEHOLDER_RE.sub("", protected.text)
                if not any(char.isalnum() for char in natural_text):
                    continue
                location = DocumentLocation(
                    part="markdown",
                    object_id=f"line:{line_number}:span:{start - offset}:{end - offset}",
                    node_ids=[f"line:{line_number}", f"span:{start}:{end}"],
                )
                data = dict(
                    document_hash=document_hash, format=DocumentFormat.MD, location=location,
                    source_language=source_language, target_language=target_language,
                    source_text=protected.text, protected_tokens=list(protected.tokens),
                    style_signature="", context_before="", context_after="",
                )
                data["id"] = generate_unit_id(**data)
                units.append(TranslationUnit.model_validate(data))
        offset += len(line)
    return _merge_hard_wrapped_units(text, units)


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
        line_match = re.fullmatch(r"line:(\d+):span:(\d+):(\d+)", location.object_id or "")
        range_match = re.fullmatch(r"range:(\d+):(\d+)", location.object_id or "")
        if line_match:
            line_number, start_in_line, end_in_line = map(int, line_match.groups())
            line_offsets = [0]
            for line in text.splitlines(keepends=True):
                line_offsets.append(line_offsets[-1] + len(line))
            if line_number >= len(line_offsets):
                errors.append(f"{unit.id}: INVALID_LOCATION")
                continue
            start = line_offsets[line_number - 1] + start_in_line
            end = line_offsets[line_number - 1] + end_in_line
        elif range_match:
            start, end = map(int, range_match.groups())
            if start < 0 or end > len(text) or start >= end:
                errors.append(f"{unit.id}: INVALID_LOCATION")
                continue
        else:
            errors.append(f"{unit.id}: INVALID_LOCATION")
            continue
        protected = _protect_inline(text[start:end])
        if protected.text != unit.source_text or list(protected.tokens) != unit.protected_tokens:
            errors.append(f"{unit.id}: SOURCE_SPAN_MISMATCH")
            continue
        if unit.id not in translations:
            errors.append(f"{unit.id}: MISSING_TRANSLATION")
            continue
        candidate = translations[unit.id]
        if candidate is None or (
            isinstance(candidate, str) and not candidate.strip()
        ) or (
            isinstance(candidate, TranslationResult) and not candidate.translation.strip()
        ):
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
        for token, original in protected.replacements:
            candidate_text = candidate_text.replace(token, original)
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
