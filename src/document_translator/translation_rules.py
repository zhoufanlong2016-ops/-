"""Machine-enforceable subset of the project's English-to-Chinese rules.

The provider receives the full writing policy.  This module covers the items
that must additionally be checked by code after every provider response.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable


_PROTECTED_PATTERNS = (
    # URLs, email addresses, file paths and CAD/Office control sequences.
    r"https?://[^\s<>()]+|mailto:[^\s<>()]+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
    r"(?:[A-Za-z]:\\|\\\\)[^\s<>\"|?*]+|(?<![\w.])(?:\./|\.\./)[\w./-]+",
    r"\\[PpHhWwFf][^;]*;",
    # Engineering numbers: stationing, dimensions, percentages, ratios,
    # tolerances, scientific notation, currency and symbol-unit combinations.
    r"\b[A-Za-z0-9&-]+(?:/[A-Za-z0-9&-]+){2,}\b",
    r"\b\d+\+\d+(?:[–-]\d+\+\d+)?\b|\b\d+:\d+\b|[ΦØ]\s*\d+|\bM\d+(?:[×x]\d+)?\b",
    r"(?<![\w.])[+-]?\d+(?:,\d{3})*(?:\.\d+)*(?:[eE][+-]?\d+)?(?:\s*(?:%|‰|°|㎡|m²|m³|mm|cm|km|m|MPa|kPa|kN|N|kg|kW|MW|W|kV|V|Hz|L|mL|USD|PKR|RMB|CNY))?",
    # Standards, document/model numbers and established engineering acronyms
    # are identifiers, not natural-language words.  Do not treat every word
    # written in title-block capitals (for example, "TOTAL TENDER PRICE") as
    # an acronym: doing so makes ordinary English headings untranslatable.
    r"\b(?:ISO|IEC|ASTM|BS|EN|AASHTO)\s*[A-Z0-9.-]+\b",
    r"\b(?:EPC|FIDIC|SCADA|ESHS|DAAB|HDPE|RCC|BOQ|BOD|COD|WWTP|STP|PPP|AIIB|CCECC|CRCC)\b",
    r"\b(?:USD|PKR|CNY|RMB|EUR|GBP|AED)\b",
    r"\b[A-Z]{1,4}\d+(?:-[A-Z0-9]+)*\b",
)
_PROTECTED_RE = re.compile("|".join(f"(?:{item})" for item in _PROTECTED_PATTERNS))
_EXISTING_PLACEHOLDER_RE = re.compile(r"(?:⟦MD_\d{4}⟧|\[\[[^\]\r\n]+\]\])$")

# Explicit geographic designators are evidence; capitalization alone is not.
_NAME_SUFFIX_RE = re.compile(
    r"\b(?:Rd\.?|Road|Street|St\.?|Highway|Hwy\.?|Avenue|Ave\.?|"
    r"Boulevard|Lane|Bridge|Colony|Town)\b\.?", re.I,
)
_NAME_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:[-'][A-Za-z0-9]+)*\b")
_NAME_BOUNDARIES = frozenset((
    "a an the at on in of for to from via along near and or between with by "
    "proposed existing new old main access service approach link local public "
    "road street highway bridge colony town construction design works widening "
    "repair rehabilitation residential industrial total tender price project "
    "crossing drainage sewer pipeline section location ds us install connect follow use see "
    "complete completed build construct widen temporary permanent river lake level training"
).split())
_ENGLISH_WORD_RE = re.compile(r"\b[A-Za-z]{2,}\b")
_DATE_RE = re.compile(r"\b(?P<day>\d{1,2})(?P<ordinal>st|nd|rd|th)\s+(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})\b", re.I)
_DATE_MONTHS = frozenset(
    "january february march april may june july august september october november december".split()
)
_DRAWING_QUALIFIERS = frozenset(("ds", "us"))


def _is_name_word(word: str) -> bool:
    return (word.istitle() or word.isupper()) and word.casefold() not in _NAME_BOUNDARIES


def proper_names(text: str) -> list[str]:
    """Recognize explicit geographic names and isolated station labels.

    This intentionally does not attempt arbitrary person/company/place NER.
    Preserve source spelling, including abbreviations and capitalization.
    """
    spans: list[tuple[int, int]] = []
    for suffix in _NAME_SUFFIX_RE.finditer(text):
        end = suffix.start()
        start = end
        for word in reversed(list(_NAME_WORD_RE.finditer(text[:end]))):
            if text[word.end():start].strip() or not _is_name_word(word.group()):
                break
            start = word.start()
            if len(_NAME_WORD_RE.findall(text[start:end])) >= 4:
                break
        if start < end:
            spans.append((start, suffix.end()))
    for prefix in re.finditer(r"\b(?:River|Lake)\b", text, re.I):
        end = prefix.end()
        for count, word in enumerate(_NAME_WORD_RE.finditer(text, end)):
            if not re.fullmatch(r"[ \t]+", text[end:word.start()]) or not _is_name_word(word.group()):
                break
            end = word.end()
            if count == 3:
                break
        if end > prefix.end():
            spans.append((prefix.start(), end))
    # DS is a drawing qualifier only when the whole line is a station label.
    # Keep it translatable, as with existing named roads/colonies followed by DS.
    for label in re.finditer(r"^[ \t]*(?P<name>[^\r\n]+?)[ \t]+DS[ \t]*\r?$", text, re.M):
        name = label.group("name")
        words = name.split()
        if 1 <= len(words) <= 4 and all(
            _NAME_WORD_RE.fullmatch(word) and _is_name_word(word) for word in words
        ):
            spans.append(label.span("name"))
    for match in re.finditer(r"\bGulshan(?:-e-|\s+e\s+)[A-Za-z]+(?:-[A-Za-z]+)*\b", text, re.I):
        if not any(left <= match.start() and right >= match.end() for left, right in spans):
            spans.append(match.span())
    return list(dict.fromkeys(text[left:right] for left, right in sorted(spans)))


def source_name_constraints(text: str, source_language: str, target_language: str) -> list[str]:
    source = source_language.strip().casefold()
    target = target_language.strip().casefold()
    if (source in {"auto", "english"} or source == "en" or source.startswith("en-")) and (
        target == "chinese" or target == "zh" or target.startswith("zh-")
    ):
        return proper_names(text)
    return []


def validate_name_retention(
    source_text: str, translation: str, source_language: str = "auto", target_language: str = "zh",
) -> list[str]:
    """Require original English name spelling at every recognized occurrence."""
    errors = []
    for name in source_name_constraints(source_text, source_language, target_language):
        pattern = r"(?<![A-Za-z])" + r"\s+".join(re.escape(word) for word in name.split()) + r"(?![A-Za-z])"
        expected = len(re.findall(pattern, source_text))
        actual = len(re.findall(pattern, translation))
        if actual < expected:
            errors.append(f"PROPER_NAME_MISSING: {name!r} expected {expected}, got {actual}; retain original English spelling")
    return errors


def validate_translation_residue(
    source_text: str,
    translation: str,
    source_language: str = "auto",
    target_language: str = "zh",
) -> list[str]:
    """Detect ordinary English left unchanged in an English-to-Chinese item."""
    source = source_language.strip().casefold()
    target = target_language.strip().casefold()
    if not ((source in {"auto", "english", "en"} or source.startswith("en-")) and (
        target in {"zh", "chinese"} or target.startswith("zh-")
    )):
        return []
    words = _ENGLISH_WORD_RE.findall(source_text)
    is_short_label = bool(
        re.search(r"\b(?:AREA|ACRE)\b", source_text, re.I)
        or re.search(r"\bLINE\s+[A-Z]\b", source_text, re.I)
        or (len(words) <= 8 and source_text.strip().isupper() and "=" in source_text)
    )
    has_date = bool(_DATE_RE.search(source_text))
    # Long paragraphs are already checked by BabelDOC's paragraph contract;
    # residue correction here is reserved for isolated drawing labels and
    # dates so it cannot turn one batch into recursive retries.
    if not is_short_label and not has_date:
        return []
    errors: list[str] = []
    remaining = source_text
    for name in sorted(source_name_constraints(source_text, source_language, target_language), key=len, reverse=True):
        remaining = re.sub(re.escape(name), " ", remaining, flags=re.I)
    for token in sorted(rule_protected_tokens(source_text), key=len, reverse=True):
        remaining = re.sub(re.escape(token), " ", remaining)
    translated = translation.casefold()
    for word in dict.fromkeys(_ENGLISH_WORD_RE.findall(remaining)):
        if word.casefold() in _DRAWING_QUALIFIERS:
            continue
        if word.casefold() in _DATE_MONTHS:
            continue
        if re.search(rf"(?<![A-Za-z]){re.escape(word)}(?![A-Za-z])", translated, re.I):
            errors.append(f"UNTRANSLATED_ENGLISH: {word!r} remains in Chinese output")
    for match in _DATE_RE.finditer(source_text):
        ordinal = match.group("day") + match.group("ordinal")
        month = match.group("month")
        if re.search(rf"(?<!\d){re.escape(ordinal)}(?![A-Za-z])", translation, re.I):
            errors.append(f"DATE_ORDINAL_UNTRANSLATED: {ordinal!r} remains; translate the ordinal")
        if month.casefold() in _DATE_MONTHS and re.search(rf"\b{re.escape(month)}\b", translation, re.I):
            errors.append(f"DATE_MONTH_UNTRANSLATED: {month!r} remains in Chinese output")
    return list(dict.fromkeys(errors))


def auto_correct_translation(
    source_text: str,
    translation: str,
    source_language: str = "auto",
    target_language: str = "zh",
) -> str:
    """Apply only deterministic fixes for drawing labels and ordinal dates."""
    source = source_language.strip().casefold()
    target = target_language.strip().casefold()
    if not ((source in {"auto", "english", "en"} or source.startswith("en-")) and (
        target in {"zh", "chinese"} or target.startswith("zh-")
    )):
        return translation
    corrected = translation
    if re.search(r"^\s*AREA\s*=", source_text, re.I) or re.search(r"\bACRE\b", source_text, re.I):
        corrected = re.sub(r"\bAREA\b", "面积", corrected, flags=re.I)
        corrected = re.sub(r"\bACRES?\b", "英亩", corrected, flags=re.I)
    line_match = re.fullmatch(r"\s*LINE\s+(?P<label>[A-Za-z0-9_-]+)\s*", source_text)
    if line_match:
        corrected = re.sub(
            rf"\bLINE\s+{re.escape(line_match.group('label'))}\b",
            f"{line_match.group('label')}线",
            corrected,
            flags=re.I,
        )
    if _DATE_RE.search(source_text) or re.search(r"\d{4}\s*[年年/]\s*\d{1,2}\s*月", corrected):
        corrected = re.sub(
            r"(?<!\d)(\d{1,2})(?:st|nd|rd|th)(?![A-Za-z])",
            r"\1日",
            corrected,
            flags=re.I,
        )
        corrected = re.sub(
            r"(?<!\d)(\d{1,2})日\s*(\d{4})[年年]\s*(\d{1,2})月",
            r"\2年\3月\1日",
            corrected,
        )
    corrected = re.sub(
        r"(?<!\d)(\d{1,2})月\s*[，,、]?\s*(\d{4})(?!\d)",
        r"\2年\1月",
        corrected,
    )
    return corrected


@dataclass(frozen=True, slots=True)
class ProtectedText:
    text: str
    replacements: tuple[tuple[str, str], ...]


def rule_protected_tokens(text: str, existing: Iterable[str] = ()) -> list[str]:
    """Return stable, non-overlapping immutable tokens in source order.

    Existing syntax placeholders (for example Markdown inline-code tokens) are
    retained.  Longest-first selection prevents a number inside a stationing
    expression from being registered twice.
    """
    candidates = [(match.start(), match.end(), match.group(0)) for match in _PROTECTED_RE.finditer(text)]
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    result = list(existing)
    occupied: list[tuple[int, int]] = []
    for start, end, value in candidates:
        if not value or any(start < right and end > left for left, right in occupied):
            continue
        occupied.append((start, end))
        if value not in result:
            result.append(value)
    return result


def protect_for_translation(text: str, tokens: Iterable[str]) -> ProtectedText:
    """Replace rule tokens with stable placeholders before a provider call.

    Existing Markdown placeholders are already opaque to providers and are
    left untouched so the Markdown adapter can restore their original syntax.
    """
    values = [token for token in tokens if token and not _EXISTING_PLACEHOLDER_RE.fullmatch(token)]
    if not values:
        return ProtectedText(text, ())
    values = sorted(set(values), key=len, reverse=True)
    pattern = re.compile("|".join(re.escape(token) for token in values))
    replacements: list[tuple[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        marker = f"[[TRP_{len(replacements):04d}]]"
        replacements.append((marker, match.group(0)))
        return marker

    return ProtectedText(pattern.sub(replace, text), tuple(replacements))


def restore_after_translation(text: str, protected: ProtectedText) -> str:
    """Restore protected source values only when every placeholder survives."""
    restored = text
    for marker, value in protected.replacements:
        marker_count = restored.count(marker)
        if marker_count == 1:
            restored = restored.replace(marker, value)
            continue
        # Some translation engines emit an immutable literal unchanged
        # instead of echoing its marker. Accept that only when the literal is
        # present exactly once; otherwise fail closed as before.
        if marker_count == 0 and restored.count(value) == 1:
            continue
        raise ValueError(f"protected placeholder was not preserved: {marker}")
    return restored
