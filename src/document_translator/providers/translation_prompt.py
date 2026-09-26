"""Shared translation instructions for engineering and contract documents."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Protocol

from document_translator.core import TranslationUnit
from document_translator.translation_rules import source_name_constraints


PROMPT_VERSION = "en-zh-general-rules-v4"


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
    return general_translation_instruction(unit.source_language, unit.target_language)


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
    # A supplied Chinese name and its retained English literal are separate
    # requirements; never send two conflicting term targets to a native API.
    mapped_sources = {source for source, _target in terms}
    terms.extend((name, name) for name in source_name_constraints(unit.source_text, unit.source_language, unit.target_language) if name not in mapped_sources)
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
    return general_translation_instruction(unit.source_language, unit.target_language)


def general_translation_instruction(source_language: str, target_language: str) -> str:
    """Return the shared policy for routes that do not own a TranslationUnit.

    PDF/BabelDOC has its own batch envelope, but it must not have its own
    linguistic rules.  Keeping this function beside ``instruction_for`` makes
    the provider and PDF prompts byte-for-byte consistent on the requirements
    that affect meaning and protected literals.
    """
    target = "English" if target_language.casefold() in {"en", "en-us", "en-gb"} else target_language
    return (
        f"Translate from {source_language} to {target}. "
        "Engineering and contract terminology takes priority over general-language translations. "
        "Inputs can be isolated drawing labels or table headers; interpret terse labels by their standard engineering drawing usage, not as conversational sentences. "
        "Keep repeated technical terms consistent, and follow supplied terminology exactly. "
        "Apply the project's English-to-Chinese general rules. Translate ordinary language naturally and accurately. "
        "For English-to-Chinese output, roads, bridges, and local place names must retain their original English spelling as the name, "
        "including Rd./Road/Street/Highway, Colony, Town, and Gulshan-e names; do not add a Chinese alias beside the retained name. "
        "Translate ordinary drawing labels, legend text, area/unit labels, line names, and dates, including AREA, ACRE, LINE, and ordinal date suffixes. "
        "Do not invent official names. Colony in a local place name means a residential neighborhood, not colonial territory. "
        "Per-item required_names are mandatory original English literals; retain each in its own item's output. "
        "Use an authoritative Chinese name for a country, place, institution, project, or company when one is supplied; "
        "otherwise transliterate people and places or preserve an unverified company, brand, product, or software name instead of inventing a literal translation. "
        "Keep defined contract terms, obligation strength, conditions, exceptions, responsibility, approvals, notices, claims, and time limits unchanged in legal effect. "
        "Preserve numbers, decimal precision, signs, ranges, currency values, dates, times, time zones, units, standards, drawing references, identifiers, protected tokens, and formatting markers. "
        "Never swap a number and its unit or identifier. Do not convert units or currencies unless explicitly requested; if conversion is requested, retain the source value. "
        "When Chinese section or list ordinals such as 一、二、三、四、第一 are not Arabic digits in the source, do not introduce Arabic numerals for them; preserve them as words or Roman numerals so they cannot collide with protected page numbers or identifiers. "
        "Keep acronyms, model numbers, formulas, URLs, file paths, codes, and CAD/Office formatting markers unchanged. "
        "Any protected token present in the source is immutable: reproduce it exactly, in the same count and binding with adjacent values or units. "
        "For an unverified proper name covered by the retained-name rule, keep the original English spelling and do not invent a Chinese alias. "
        "When the input contains <UNIT_n> markers, return every marker unchanged and keep its translated text under that marker. "
        "Output only the translation, with no explanation. "
        "Translate the complete input without summarizing, abbreviating, omitting clauses, or using ellipses (... or …). Preserve every sentence and list item. "
        "For English output, use parentheses instead of em dashes, ordinary hyphens only, and leave no Chinese characters."
    )
