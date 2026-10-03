"""Deterministic checks that never mutate translation text."""

from collections import Counter
import re
from typing import Iterable

from .models import TranslationResult, TranslationUnit, sha256_text
from document_translator.translation_rules import validate_name_retention, validate_translation_residue


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
    # A list label is protected with the spaces around it (" 1. "), which
    # Chinese output drops ("注：1.未注明…"); the label itself is what counts.
    if token.strip() and token != token.strip():
        token = token.strip()
    # Hierarchical list labels may legitimately touch their following word in
    # the source (for example, ``2.2.1Design``).  Count the full label
    # literally so normalising the following spacing in translation does not
    # create a false mismatch.
    if re.fullmatch(r"\d+(?:\.\d+){1,}", token):
        return text.count(token)
    # A numeric or engineering identifier must not be accepted as a substring
    # of a changed value, for example ``2015`` inside ``202015``.
    # "3,500 mm" may be written "3,500mm" in Chinese output (no space
    # between a number and its unit there); both are the same value.
    from document_translator.translation_rules import UNIT_WORDS

    unit = re.fullmatch(rf"(.*\d)[ \t]+((?:{UNIT_WORDS}))", token)
    if unit:
        pattern = rf"(?<![A-Za-z0-9]){re.escape(unit.group(1))}[ \t]?{re.escape(unit.group(2))}(?![A-Za-z])"
        return len(re.findall(pattern, text))
    if token[0].isdigit() or token[-1].isdigit():
        # A list label such as "1. " already ends in a space; demanding a
        # non-alphanumeric after it rejected "1. Kindly" in the source while
        # accepting "1. 请" in the translation.
        # A unit may follow directly in Chinese output ("150HP", "132KV").
        # A token ending in a non-alphanumeric ("2026年1月15日") needs no
        # boundary after it ("2026年1月15日12:00时").
        tail = "" if not (token[-1].isascii() and token[-1].isalnum()) else rf"(?:(?![A-Za-z0-9])|(?=(?:{UNIT_WORDS}|KV)(?![A-Za-z])))"
        pattern = rf"(?<![A-Za-z0-9]){re.escape(token)}{tail}"
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
    # A provider occasionally returns a blank (or whitespace-only) string as
    # a "valid" translation for a very short, fragment-like unit -- observed
    # directly on a real document whose layout splits a short trailing
    # clause ("...as shown in Table 1" / "所示:") off into its own tiny
    # unit. Nothing previously treated this as an error, so it sailed
    # through the same "keep the best-effort translation" path as any other
    # unit; for most formats that just leaves a blank paragraph, but MinerU's
    # own middle_json schema requires non-empty text content for every
    # block, so writing this straight back in crashed the ENTIRE PDF with
    # an unrelated-looking Pydantic error at final serialization -- one odd
    # short unit took down the whole document instead of just itself.
    # Flagging it here feeds the existing remediation-retry path (unit.py's
    # callers already give the same model one more attempt at anything with
    # errors), and a caller that still gets an empty result after that is
    # expected to fall back to the unit's own source text rather than ever
    # writing blank content into the document.
    if unit.source_text.strip() and not result.translation.strip():
        errors.append("EMPTY_TRANSLATION: provider returned blank text for non-blank source")
    # Markers are restored before validation, so one left in the text was
    # copied from another item of the batch ("直径大[[TRP_0001]]。" for a
    # line with no protected values); it must never reach a document.
    leaked = sorted(set(re.findall(r"\[\[TRP_\d+\]\]", result.translation)) - set(re.findall(r"\[\[TRP_\d+\]\]", unit.source_text)))
    if leaked:
        errors.append(f"MARKER_LEAK: {', '.join(leaked)} is not part of this item")
    incomplete = _incomplete_translation(unit, result.translation)
    if incomplete:
        errors.append(incomplete)
    errors.extend(validate_placeholders(unit.source_text, result.translation, unit.protected_tokens))
    errors.extend(validate_name_retention(unit.source_text, result.translation, unit.source_language, unit.target_language))
    errors.extend(validate_translation_residue(unit.source_text, result.translation, unit.source_language, unit.target_language))
    if unit.target_language.lower().startswith("en") and _LITERAL_DATE_RE.search(result.translation):
        errors.append("LITERAL_DATE: 年/月/日 rendered word for word instead of as an English date")
    return errors


def _incomplete_translation(unit: TranslationUnit, translation: str) -> str | None:
    """Flag a translation far too short for its source.

    Nothing caught a model translating only part of a unit: a 600-character
    table cell (two list items) came back as one 85-character sentence. A
    complete English-to-Chinese translation runs a quarter to a third of the
    source's length (0.23-0.3 measured on the tender documents), Chinese-to-English two to three times; well below that, text
    is missing.
    """
    source, target = unit.source_language.lower(), unit.target_language.lower()
    length = len(unit.source_text.strip())
    produced = len(translation.strip())
    if source.startswith("en") and target.startswith("zh") and length >= 150 and produced < 0.17 * length:
        return f"TRANSLATION_INCOMPLETE: {produced} characters for a {length}-character source"
    if source.startswith("zh") and target.startswith("en") and length >= 60 and produced < 1.0 * length:
        return f"TRANSLATION_INCOMPLETE: {produced} characters for a {length}-character source"
    return None


# "12 Month, Day 5, 2024" -- a Chinese date translated character by
# character. Real English never puts a bare number next to the word
# "Month"/"Day" this way.
_LITERAL_DATE_RE = re.compile(r"\b\d{1,2}\s+Month\b|\bMonth\s*,?\s*Day\b|\bDay\s+\d{1,2}\b|\b\d{4}\s+Year\b")


def validate_glossary_terms(
    source_text: str,
    translation: str,
    terms: Iterable[tuple[str, str]],
) -> list[str]:
    """Require every matched glossary target to survive translation verbatim."""
    errors: list[str] = []
    for source, target in terms:
        # Multi-word English terms match in any case, as in Glossary.entries_for().
        folded = not re.search(r"[㐀-鿿]", source) and len(source.split()) >= 2
        expected = source_text.casefold().count(source.casefold()) if folded else source_text.count(source)
        actual = translation.casefold().count(target.casefold())
        if expected and actual < expected:
            errors.append(
                f"GLOSSARY_TERM_MISSING: {source!r} -> {target!r} expected {expected}, "
                f"got {actual}"
            )
    return errors
