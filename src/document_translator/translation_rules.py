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
    r"(?:[A-Za-z]:\\|\\\\)[^\s<>\"|?*]+|(?:\./|\.\./)[\w./-]+",
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
