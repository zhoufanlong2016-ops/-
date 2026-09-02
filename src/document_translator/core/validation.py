"""Deterministic checks that never mutate translation text."""

from collections import Counter

from .models import TranslationResult, TranslationUnit, sha256_text


def validate_placeholders(source_text: str, translation: str, protected_tokens: list[str]) -> list[str]:
    """Return errors when token content, case, or counts differ."""
    source_counts = Counter({token: source_text.count(token) for token in protected_tokens})
    result_counts = Counter({token: translation.count(token) for token in protected_tokens})
    return [
        f"PLACEHOLDER_MISMATCH: {token!r} expected {source_counts[token]}, got {result_counts[token]}"
        for token in protected_tokens if result_counts[token] != source_counts[token]
    ]


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
