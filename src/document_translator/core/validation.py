"""Deterministic checks that never mutate translation text."""

from collections import Counter
import re
from typing import Iterable

from .models import TranslationResult, TranslationUnit, sha256_text


def validate_placeholders(source_text: str, translation: str, protected_tokens: list[str]) -> list[str]:
    """Return errors when token content, case, or counts differ."""
    source_counts = Counter({token: _count_token(source_text, token) for token in protected_tokens})
    result_counts = Counter({token: _count_token(translation, token) for token in protected_tokens})
    return [
        f"PLACEHOLDER_MISMATCH: {token!r} expected {source_counts[token]}, got {result_counts[token]}"
        for token in protected_tokens if result_counts[token] != source_counts[token]
    ]


def _count_token(text: str, token: str) -> int:
    """Count identifier occurrences without accepting a digit glued to a token."""
    # Formatting sentinels are intentionally glued to neighbouring text.
    if token.startswith(("⟦", "[[")):
        return text.count(token)
    # Hierarchical list labels may legitimately touch their following word in
    # the source (for example, ``2.2.1Design``).  Count the full label
    # literally so normalising the following spacing in translation does not
    # create a false mismatch.
    if re.fullmatch(r"\d+(?:\.\d+){1,}", token):
        return text.count(token)
    # A numeric or engineering identifier must not be accepted as a substring
    # of a changed value, for example ``2015`` inside ``202015``.
    if token[0].isdigit() or token[-1].isdigit():
        pattern = rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])"
        return len(re.findall(pattern, text))
    return text.count(token)


def validate_result_for_unit(unit: TranslationUnit, result: TranslationResult) -> list[str]:
    errors: list[str] = []
    if result.unit_id != unit.id:
        errors.append("UNIT_ID_MISMATCH")
    if result.source_hash != sha256_text(unit.source_text):
        errors.append("SOURCE_HASH_MISMATCH")
    if result.result_hash != sha256_text(result.translation):
        errors.append("RESULT_HASH_MISMATCH")
    errors.extend(validate_placeholders(unit.source_text, result.translation, unit.protected_tokens))
    return errors


def validate_glossary_terms(
    source_text: str,
    translation: str,
    terms: Iterable[tuple[str, str]],
) -> list[str]:
    """Require every matched glossary target to survive translation verbatim."""
    errors: list[str] = []
    for source, target in terms:
        expected = source_text.count(source)
        actual = translation.casefold().count(target.casefold())
        if expected and actual < expected:
            errors.append(
                f"GLOSSARY_TERM_MISSING: {source!r} -> {target!r} expected {expected}, "
                f"got {actual}"
            )
    return errors
