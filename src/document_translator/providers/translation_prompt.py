"""Shared translation instructions for engineering and contract documents."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol

from document_translator.core import TranslationUnit


PROMPT_VERSION = "en-zh-general-rules-v3"


class GlossaryLike(Protocol):
    def entries_for(self, text: str): ...


def matched_glossary_entries(unit: TranslationUnit, glossary: GlossaryLike | None):
    """Return only glossary entries whose script direction matches the job."""
    if glossary is None:
        return ()
    entries = glossary.entries_for(unit.source_text)
    source = unit.source_language.casefold()
    target = unit.target_language.casefold()
    source_is_cjk = source.startswith(("zh", "ja", "ko"))
    target_is_cjk = target.startswith(("zh", "ja", "ko"))
    cjk = lambda value: bool(re.search(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", value))
    filtered = []
    for entry in entries:
        entry_source_cjk = cjk(entry.source)
        entry_target_cjk = cjk(entry.target)
        if source_is_cjk and not target_is_cjk and (not entry_source_cjk and entry_target_cjk):
            continue
        if not source_is_cjk and target_is_cjk and (entry_source_cjk and not entry_target_cjk):
            continue
        filtered.append(entry)
    return tuple(filtered)


@dataclass(frozen=True, slots=True)
class CompiledTranslationPolicy:
    """Provider-neutral, executable form of the English-to-Chinese rules."""

    instruction: str
    qwen_domain: str
    required_terms: tuple[tuple[str, str], ...]


def qwen_domain_instruction(unit: TranslationUnit) -> str:
    """Qwen-MT's supported replacement for a system translation prompt.

    The Qwen-MT endpoint accepts one source user message only.  Its documented
    ``translation_options.domains`` field is therefore the only safe location
    for provider-side rules; it deliberately contains no source document text.
    """
    return (
        "This is an engineering, procurement, contract, and CAD/Office document. "
        f"Translate from {unit.source_language} to {unit.target_language}. Preserve legal effect, "
        "defined terms, obligation strength, conditions, exceptions, responsibilities, "
        "approvals, notices, claims, and time limits. Preserve every number with its "
        "unit, date, currency, range, standard, drawing reference, identifier, URL, "
        "path, formula, acronym, model number, and supplied term exactly. Do not convert "
        "units or currencies. Use supplied terminology exactly. Keep verified official "
        "names; otherwise retain an unverified company, brand, product, or software name "
        "rather than inventing one. Return translation only."
    )


def compile_translation_policy(unit: TranslationUnit, glossary: GlossaryLike | None = None) -> CompiledTranslationPolicy:
    """Compile one immutable policy for prompts, Qwen options, and validation.

    Rules are deliberately compiled once from the same unit facts.  A provider
    may choose a different transport, but cannot silently omit glossary terms
    or immutable source literals.
    """
    terms: list[tuple[str, str]] = []
    if glossary is not None:
        terms.extend((entry.source, entry.target) for entry in matched_glossary_entries(unit, glossary))
    terms.extend((token, token) for token in unit.protected_tokens)
    deduplicated = tuple(dict.fromkeys(terms))
    instruction = instruction_for(unit)
    if deduplicated:
        instruction += "\n\nRequired terminology and immutable literals:\n" + "\n".join(
            f"- {source} -> {target}" for source, target in deduplicated
        )
    return CompiledTranslationPolicy(
        instruction=instruction,
        qwen_domain=qwen_domain_instruction(unit),
        required_terms=deduplicated,
    )


def instruction_for(unit: TranslationUnit) -> str:
    """Return the fixed translation policy for every provider request."""
    target = "English" if unit.target_language.casefold() in {"en", "en-us", "en-gb"} else unit.target_language
    return (
        f"Translate from {unit.source_language} to {target}. "
        "Engineering and contract terminology takes priority over general-language translations. "
        "Inputs can be isolated drawing labels or table headers; interpret terse labels by their standard engineering drawing usage, not as conversational sentences. "
        "Keep repeated technical terms consistent, and follow supplied terminology exactly. "
        "Apply the project's English-to-Chinese general rules. Translate ordinary language naturally and accurately. "
        "Use an authoritative Chinese name for a country, place, institution, project, or company when one is supplied; "
        "otherwise transliterate people and places or preserve an unverified company, brand, product, or software name instead of inventing a literal translation. "
        "Keep defined contract terms, obligation strength, conditions, exceptions, responsibility, approvals, notices, claims, and time limits unchanged in legal effect. "
        "Preserve numbers, decimal precision, signs, ranges, currency values, dates, times, time zones, units, standards, drawing references, identifiers, protected tokens, and formatting markers. "
        "Never swap a number and its unit or identifier. Do not convert units or currencies unless explicitly requested; if conversion is requested, retain the source value. "
        "When Chinese section or list ordinals such as 一、二、三、四、第一 are not Arabic digits in the source, do not introduce Arabic numerals for them; preserve them as words or Roman numerals so they cannot collide with protected page numbers or identifiers. "
        "Keep acronyms, model numbers, formulas, URLs, file paths, codes, and CAD/Office formatting markers unchanged. "
        "Any protected token present in the source is immutable: reproduce it exactly, in the same count and binding with adjacent values or units. "
        "For a first occurrence of an unverified proper name, use a cautious Chinese transliteration followed by the original English in parentheses; do not invent a company or brand translation. "
        "When the input contains <UNIT_n> markers, return every marker unchanged and keep its translated text under that marker. "
        "Output only the translation, with no explanation. "
        "Translate the complete input without summarizing, abbreviating, omitting clauses, or using ellipses (... or …). Preserve every sentence and list item. "
        "For English output, use parentheses instead of em dashes, ordinary hyphens only, and leave no Chinese characters."
    )
