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
    # "3-D"/"2D" is one notation: protecting only its digit made "3-D model"
    # -> "3D模型" a placeholder mismatch ("3" no longer stood alone).
    r"(?<![A-Za-z0-9-])[234]-?D(?![A-Za-z0-9])",
    # A table's own row-number cell (just "1.", "2)", nothing else) is a
    # bare numbering marker, not a sentence -- it has no full stop to
    # localise. Left unprotected, providers commonly "translate" its
    # ASCII "." into the Chinese full-width "。" anyway (observed on both
    # this project's own providers), which is wrong on its own terms
    # (a list index is not prose) and additionally exposed a real font
    # rendering defect for that character on this project's own render
    # path. Anchored to the whole cell (a bare marker is never legitimately
    # only PART of a larger cell's text) so this never touches a period or
    # bracket appearing inside actual sentence content elsewhere. Placed
    # ahead of the general engineering-number pattern below: alternation
    # tries earlier branches first and keeps whichever matches first, not
    # whichever is longest, so a later, narrower rule never gets a chance
    # to win against an earlier one that already matched just the bare
    # digit.
    r"^\s*\d{1,3}[.)）]\s*$",
    # The SAME numbering marker also appears leading a normal paragraph of
    # real content ("1. Kindly refer to Section 6...") rather than filling
    # a whole cell alone -- observed rendering as "1。请参阅..." with the
    # marker's own period turned into a Chinese full stop even though
    # everything after it translated correctly. Only the marker itself
    # (through the whitespace right after it) is protected here, so the
    # sentence that follows still reaches the model normally.
    r"^\s*\d{1,3}[.)）]\s+",
    # The same marker also starts a NEW numbered item midway through a
    # cell's own long text, not just at the very beginning of it -- table
    # cells go through mineru_pdf._translate_tables(), which sends a
    # cell's ENTIRE multi-paragraph text to the provider as one string, so
    # a genuine new list item's own "2." can land anywhere inside that
    # string, not only at position zero (observed: "...avoid water
    # flooding live switchgear. 2. For the offices..." rendered with that
    # second marker's period turned into "。" exactly like the leading
    # case above). Requiring real sentence-ending punctuation right before
    # it is what distinguishes "a new item is starting here" from an
    # ordinary decimal or a mid-sentence number: "2.3" or "item 5" never
    # match, since neither has one of these right before the digit.
    r"(?<=[.!?:;])\s+\d{1,3}[.)）]\s+",
    # Engineering numbers: stationing, dimensions, percentages, ratios,
    # tolerances, scientific notation, currency and symbol-unit combinations.
    r"\b[A-Za-z0-9&-]+(?:/[A-Za-z0-9&-]+){2,}\b",
    r"\b\d+\+\d+(?:[–-]\d+\+\d+)?\b|\b\d+:\d+\b|[ΦØ]\s*\d+|\bM\d+(?:[×x]\d+)?\b",
    # An atomic group stops the engine from retrying a shorter digit run
    # when the ordinal exclusion below fails on the full run (otherwise
    # "17th" would backtrack to protecting just "1", still leaving "7th"
    # glued to the placeholder). A bare ordinal ("17th", "3rd") must stay
    # out of protection entirely so the whole phrase reaches the model
    # intact instead of being split into a placeholder plus a stray suffix.
    # The unit suffix must sit on the SAME line as its number: allowing
    # \s* (which matches a newline) here let a number at the end of one
    # wrapped line glue onto a single capital letter starting the next
    # line whenever that letter also happens to be a unit symbol (V, N,
    # W, L, m...) -- for example "...Item 72\nVolume-1..." was matching
    # as "72\nV" ("72 Volts"), consuming the V and silently defeating
    # the immutable-identifier pattern below for "Volume-1". A real
    # "number unit" pairing never has a hard line break between them.
    # The trailing (?![A-Za-z]) guards the same failure mode on a single
    # line: without it a short unit letter (N, W, V, m...) also matches
    # as the FIRST letter of an unrelated following word -- "1 No x 4
    # Cusec" protected "1 N" as "1 Newton", leaving a stray "o" glued to
    # the placeholder that the model could never restore, observed as a
    # PLACEHOLDER_MISMATCH on a lift-station schedule where "No" (quantity)
    # appears on nearly every line. A genuine unit is never immediately
    # followed by another Latin letter (a Chinese character or digit
    # right after, as in "450kW光伏", is unaffected).
    # kVA/KVA (apparent power -- transformer and generator ratings) must be
    # tried before the shorter kV alternative below, or "1000KVA" matches
    # only "1000kV" and leaves a stray "A". It is spelled both ways in the
    # wild (kVA is the SI-correct form; KVA is what most drawing title
    # blocks actually use), and unlike every other unit here it is also
    # written glued to its number with NO space ("1000KVA") about as often
    # as with one ("1000 KVA") in the same table -- the two spellings must
    # both be protected as one token, or _count_token()'s digit-boundary
    # check (core/validation.py) counts the bare number in one spelling
    # but not the other, and a plain reformatting difference between the
    # two forms is misreported as a placeholder mismatch.
    r"(?<![\w.])[+-]?(?>\d+)(?!(?:st|nd|rd|th)\b)(?:,\d{3})*(?:\.\d+)*(?:[eE][+-]?\d+)?(?:[ \t]*(?:kVA|KVA|%|‰|°|㎡|m²|m³|mm|cm|km|m|MPa|kPa|kN|N|kg|kW|MW|W|kV|V|Hz|L|mL|USD|PKR|RMB|CNY)(?![A-Za-z]))?",
    # Standards, document/model numbers and established engineering acronyms
    # are identifiers, not natural-language words.  Do not treat every word
    # written in title-block capitals (for example, "TOTAL TENDER PRICE") as
    # an acronym: doing so makes ordinary English headings untranslatable.
    # A mandatory space plus a digit in the code keeps this from also
    # matching ordinary English words that start with "EN"/"BS" (for
    # example "ENVELOPE", "ENGINEERING", "BSSN"): a real standard reference
    # is always an abbreviation, a space, and a code containing a number.
    r"\b(?:ISO|IEC|ASTM|BS|EN|AASHTO)\s+[A-Z]*\d[A-Z0-9.-]*\b",
    # ASCII boundaries, not \b: Chinese characters count as word characters,
    # so "EPC银皮书" or "AIIB项目" was never protected and a model could drop it.
    r"(?<![A-Za-z0-9])(?:EPC|FIDIC|SCADA|ESHS|DAAB|HDPE|RCC|BOQ|BOD|COD|WWTP|STP|PPP|AIIB|CCECC|CRCC)(?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])(?:USD|PKR|CNY|RMB|EUR|GBP|AED)(?![A-Za-z0-9])",
    r"\b[A-Z]{1,4}\d+(?:-[A-Z0-9]+)*\b",
    # A hyphen-joined compound identifier ("Volume-1", "LW-TD-411") whose
    # ASCII spelling must survive translation. This mirrors
    # pdf_pipeline.extract_immutable_identifiers(), which validates the
    # SAME shape after translation but was never fed into this
    # PRE-translation protection set -- an identifier that only that
    # post-hoc check recognised could still reach the model unprotected
    # and come back translated, so validation only ever caught the
    # failure after the fact instead of preventing it.
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9-]*\d)[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+(?![A-Za-z0-9])",
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
_IZAFAT_NAME_RE = re.compile(r"\b[A-Z][a-z]+(?:[- ]e[- ][A-Z][a-z]+)+\b")
_DATE_RE = re.compile(r"\b(?P<day>\d{1,2})(?P<ordinal>st|nd|rd|th)\s+(?P<month>[A-Za-z]+)\s+(?P<year>\d{4})\b", re.I)
_DATE_MONTHS = frozenset(
    "january february march april may june july august september october november december".split()
)
_DATE_MONTH_NUMBERS = {
    name: index
    for index, name in enumerate(
        "january february march april may june july august september october november december".split(),
        start=1,
    )
}
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
    # The name is optional in the pattern itself: a caption is sometimes
    # wrapped so far that the DS-suffix line is bare ("SHALIMAR\nDS"),
    # with the entire name living on the line above.
    for label in re.finditer(r"^[ \t]*(?:(?P<name>[^\r\n]+?)[ \t]+)?DS[ \t]*\r?$", text, re.M):
        name = label.group("name")
        words = name.split() if name else []
        if words and not (len(words) <= 4 and all(
            _NAME_WORD_RE.fullmatch(word) and _is_name_word(word) for word in words
        )):
            continue
        if name is not None:
            start, end = label.span("name")
        else:
            start = end = label.start()
        # A drawing-page pin caption is sometimes wrapped onto the line
        # above ("CENTER\nPOINT DS"): the DS-suffix line alone names
        # only its own last word (or, for a bare "DS" line, no word at
        # all), leaving the wrapped first line as ordinary translatable
        # English and splitting one place name in two. Extend protection
        # back across a single preceding line when that whole line is
        # itself made of name-shaped words.
        line_start = start
        while line_start > 0 and text[line_start - 1] in "\r\n":
            line_start -= 1
        if name is None:
            end = line_start
        prev_start = text.rfind("\n", 0, line_start) + 1
        prev_line = text[prev_start:line_start]
        prev_words = prev_line.split()
        extended = prev_words and len(prev_words) + len(words) <= 4 and all(
            _NAME_WORD_RE.fullmatch(word) and _is_name_word(word) for word in prev_words
        )
        if extended:
            start = prev_start
        elif not words:
            # A bare "DS" line with nothing name-shaped above it is not
            # a station label at all; leave it alone.
            continue
        spans.append((start, end))
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


_LOWER_RUN_RE = re.compile(r"\b[a-z]+(?:[ \t]+[a-z]+){3,}\b")


def _untranslated_phrase(source_text: str, translation: str) -> str | None:
    source = " ".join(source_text.split()).casefold()
    for match in _LOWER_RUN_RE.finditer(translation):
        if match.group(0) in source:
            return match.group(0)
    return None


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
    # Long paragraphs are already checked by the structured PDF block contract;
    # residue correction here is reserved for isolated drawing labels and
    # dates so it cannot turn one batch into recursive retries.
    #
    # A table cell's own long paragraph was tried here too (this project's
    # engineering-clarification tables are full of them, and one dropped
    # "domestic sewage" untranslated with nothing positioned to catch it),
    # but reverted immediately: this document's tables are equally full of
    # legitimate bare acronyms and units the writing policy deliberately
    # keeps in English -- NFPA, GSM, PMC, WASA, MV, ER, SLD, mm, and more
    # -- and neither the protected-token nor the proper-name filter below
    # was ever meant to enumerate that class, only numbers/identifiers and
    # multi-word names. Enabling this check there flagged nearly every
    # cell, forcing far more remediation retries than intended; one of
    # those independent retries then regressed a "Gulshan e Ravi" mention
    # that had translated correctly the first time -- a net loss, not a
    # fix. Left as a known gap rather than trading it for that.
    # Prose left in English is caught by phrase, not by word: four or more
    # lowercase words in a row from the source ("of proportion of each site
    # on delivery") are an untranslated sentence, while acronyms and names
    # (PMC, Gulshan Ravi) never form such a run.
    phrase = _untranslated_phrase(source_text, translation)
    if not is_short_label and not has_date:
        return [f"UNTRANSLATED_ENGLISH: {phrase!r} remains in Chinese output"] if phrase else []
    errors: list[str] = [f"UNTRANSLATED_ENGLISH: {phrase!r} remains in Chinese output"] if phrase else []
    remaining = source_text
    # "Gulshan-e-Ravi" / "Gulshan e Ravi" is a place name the prompt tells the
    # model to keep in English; it has no Rd./Colony suffix to be recognised by.
    remaining = _IZAFAT_NAME_RE.sub(" ", remaining)
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
        # Acronyms (PMC, GSM, LDA, WASA) stay in English by policy; in an
        # all-caps drawing label ("AREA= 86 ACRE") capitals mark nothing.
        if word.isupper() and len(word) <= 6 and not source_text.isupper():
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


_ZH_CHAR = r"㐀-鿿豈-﫿"
_ZH_PUNCT = r"　-〿！-／：-＠［-｀｛-･‘’“”…—"
_SPACE = r"[ \t 　]+"
_ZH_SPACING_RES = (
    # 汉字之间:"业主 要求"
    re.compile(rf"(?<=[{_ZH_CHAR}]){_SPACE}(?=[{_ZH_CHAR}])"),
    # 汉字 + 数字/英文:"第 1 卷"、"至 LW-TD-424"
    re.compile(rf"(?<=[{_ZH_CHAR}]){_SPACE}(?=[A-Za-z0-9(\[<'\"])"),
    re.compile(rf"(?<=[A-Za-z0-9)\]>%'\".]){_SPACE}(?=[{_ZH_CHAR}])"),
    # 中文标点前后:"， 因此"、"规定 。"
    re.compile(rf"(?<=[{_ZH_PUNCT}]){_SPACE}"),
    re.compile(rf"{_SPACE}(?=[{_ZH_PUNCT}])"),
)
# A list label keeps its space ("1. 请参阅"): it is a protected token.
_LIST_LABEL_BEFORE_RE = re.compile(r"(?:^|\n)[ \t]*(?:\(?\d{1,3}[.)]|\(?[A-Za-z][.)]|[-*•])$")


def normalize_chinese_spacing(text: str, target_language: str) -> str:
    """Chinese output carries no spaces between 汉字 and digits or Latin
    letters, nor around Chinese punctuation ("第 1 卷，第一部分，第 2 节"
    -> "第1卷，第一部分，第2节"). Spaces inside English or between a
    word and a number ("ISO 9001") are left as the source has them."""
    if not target_language.strip().casefold().startswith(("zh", "chinese")):
        return text
    for pattern in _ZH_SPACING_RES:
        text = pattern.sub(lambda match: match.group(0) if _LIST_LABEL_BEFORE_RE.search(text[: match.start()]) else "", text)
    return _NUMBER_UNIT_SPACE_RE.sub("", text)


# 数字 + 英文单位:"3,500 mm" -> "3,500mm"
UNIT_WORDS = (
    "kVA|KVA|MPa|kPa|kN|kg|kW|MW|kV|KV|Hz|mL|mm|cm|km|m²|m³|㎡|m|N|W|V|L|HP|hp|hrs|hr|"
    "cusecs|cusec|Cusecs|Cusec|USD|PKR|RMB|CNY|%|‰|°|"
    # psi, apparent power, energy, flow and speed units ("6000 psi", "450 MGD")
    "psi|PSI|MVA|kWh|MWh|MGD|cfs|rpm|sqm"
)
_NUMBER_UNIT_SPACE_RE = re.compile(rf"(?<=\d)[ \t ]+(?=(?:{UNIT_WORDS})(?![A-Za-z]))")


# The marker may touch its text ("1.Increased": label and text set apart
# by position, not by a space); a decimal ("1.5") is not a marker.
_LIST_MARKER_RE = re.compile(r"(?m)^(\s*)(\d{1,3})([.)])(?=\s|$|[^\W\d_])")


def restore_list_markers(source_text: str, translation: str) -> str:
    """A list number keeps its own mark: "1." is not a sentence, and a model
    still wrote "1。" although the marker was protected. Each line that
    starts a numbered item in the source gets the source's mark back."""
    marks = {match.group(2): match.group(3) for match in _LIST_MARKER_RE.finditer(source_text)}
    if not marks:
        return translation

    def fix(match: re.Match[str]) -> str:
        mark = marks.get(match.group(2))
        return f"{match.group(1)}{match.group(2)}{mark}" if mark else match.group(0)

    return re.sub(r"(?m)^(\s*)(\d{1,3})[。．、]", fix, translation)


def auto_correct_translation(
    source_text: str,
    translation: str,
    source_language: str = "auto",
    target_language: str = "zh",
) -> str:
    """Apply only deterministic fixes for drawing labels and ordinal dates."""
    translation = restore_list_markers(source_text, translation)
    source = source_language.strip().casefold()
    target = target_language.strip().casefold()
    if not ((source in {"auto", "english", "en"} or source.startswith("en-")) and (
        target in {"zh", "chinese"} or target.startswith("zh-")
    )):
        return normalize_chinese_spacing(translation, target_language)
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
        for match in _DATE_RE.finditer(source_text):
            month_number = _DATE_MONTH_NUMBERS.get(match.group("month").casefold())
            if month_number is not None:
                corrected = re.sub(
                    rf"(?<![A-Za-z]){re.escape(match.group('month'))}(?![A-Za-z])",
                    f"{month_number}月",
                    corrected,
                    flags=re.I,
                )
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
            r"(?<!\d)(\d{1,2})日\s*(\d{1,2})月\s*(\d{4})(?!\d)",
            r"\3年\2月\1日",
            corrected,
        )
        for match in _DATE_RE.finditer(source_text):
            # Some models drop the ordinal suffix and month name entirely,
            # leaving a bare number sequence (for example "2025 12 17")
            # with nothing left for the substitutions above to anchor on.
            # Rebuild the Chinese date directly from the three numbers the
            # source match already gives us, in either field order.
            month_number = _DATE_MONTH_NUMBERS.get(match.group("month").casefold())
            if month_number is None:
                continue
            day, year = match.group("day"), match.group("year")
            target = f"{year}年{month_number}月{day}日"
            bare_ymd = rf"(?<!\d){re.escape(year)}[\s,/.-]+{month_number}[\s,/.-]+{day}(?!\d)(?!日)"
            bare_dmy = rf"(?<!\d){day}[\s,/.-]+{month_number}[\s,/.-]+{re.escape(year)}(?!\d)"
            corrected = re.sub(bare_ymd, target, corrected)
            corrected = re.sub(bare_dmy, target, corrected)
    corrected = re.sub(
        r"(?<!\d)(\d{1,2})月\s*[，,、]?\s*(\d{4})(?!\d)",
        r"\2年\1月",
        corrected,
    )
    return normalize_chinese_spacing(corrected, target_language)


@dataclass(frozen=True, slots=True)
class ProtectedText:
    text: str
    replacements: tuple[tuple[str, str], ...]


_EN_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
# Whitespace is tolerated between every part: PDF extraction turns the
# source's own justified character spacing into spaces ("2024 年12 月5 日").
_CN_FULL_DATE_RE = re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
_CN_YEAR_MONTH_RE = re.compile(r"(?<!\d)(\d{4})\s*年\s*(\d{1,2})\s*月(?!\s*\d)")
_CN_MONTH_DAY_RE = re.compile(r"(?<![\d年])(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")


def localize_chinese_dates(text: str, source_language: str, target_language: str) -> tuple[str, list[str]]:
    """Replace Chinese numeric dates with their one correct English form.

    A Chinese date has exactly one English rendering, so it is never left
    to the model: sent as-is, the numeric-token protection turned the year
    into an opaque placeholder ("[[TRP_0000]] 年12 月5 日") and providers
    produced "12 Month, Day 5, 2024" or dropped 年/月/日 entirely ("2024 12
    5"), neither of which any check caught. Returns the rewritten text and
    the English dates it inserted, which the caller protects so the model
    must keep them verbatim. Only applies to Chinese-to-English units.
    """
    if source_language.lower().startswith("en") and target_language.lower().startswith("zh"):
        return _localize_english_dates(text)
    if not source_language.lower().startswith("zh") or not target_language.lower().startswith("en"):
        return text, []
    dates: list[str] = []

    def emit(value: str) -> str:
        if value not in dates:
            dates.append(value)
        return value

    def full(match: re.Match[str]) -> str:
        year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return match.group(0)
        return emit(f"{_EN_MONTH_NAMES[month - 1]} {day}, {year}")

    def year_month(match: re.Match[str]) -> str:
        year, month = int(match.group(1)), int(match.group(2))
        if not 1 <= month <= 12:
            return match.group(0)
        return emit(f"{_EN_MONTH_NAMES[month - 1]} {year}")

    def month_day(match: re.Match[str]) -> str:
        month, day = int(match.group(1)), int(match.group(2))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return match.group(0)
        return emit(f"{_EN_MONTH_NAMES[month - 1]} {day}")

    text = _CN_FULL_DATE_RE.sub(full, text)
    text = _CN_YEAR_MONTH_RE.sub(year_month, text)
    text = _CN_MONTH_DAY_RE.sub(month_day, text)
    return text, dates


_EN_MONTH_PATTERN = (
    r"(?P<month>Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
    r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
_EN_MONTH_DAY_YEAR_RE = re.compile(
    r"\b" + _EN_MONTH_PATTERN + r"\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<year>\d{4})\b", re.I
)
_EN_DAY_MONTH_YEAR_RE = re.compile(
    r"\b(?P<day>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?" + _EN_MONTH_PATTERN + r",?\s+(?P<year>\d{4})\b", re.I
)


def _localize_english_dates(text: str) -> tuple[str, list[str]]:
    """English full dates -> their one Chinese form, decided here, not by the model.

    With the day and year protected as opaque placeholders the model could
    not tell them apart: "January 29, 2026" came back as "2026年29月"
    (January dropped, the day used as the month).
    """
    dates: list[str] = []

    def emit(match: re.Match[str]) -> str:
        month = next(i for i, name in enumerate(_EN_MONTH_NAMES, start=1) if name.lower().startswith(match.group("month").lower()[:3]))
        day, year = int(match.group("day")), int(match.group("year"))
        if not 1 <= day <= 31:
            return match.group(0)
        value = f"{year}年{month}月{day}日"
        if value not in dates:
            dates.append(value)
        return value

    text = _EN_MONTH_DAY_YEAR_RE.sub(emit, text)
    text = _EN_DAY_MONTH_YEAR_RE.sub(emit, text)

    # "Volume-2" reads as an identifier, was protected as one and came back
    # in English ("Volume-2，第二部分"); a volume number has one Chinese form.
    def volume(match: re.Match[str]) -> str:
        value = f"第{match.group(1)}卷"
        if value not in dates:
            dates.append(value)
        return value

    text = _EN_VOLUME_RE.sub(volume, text)
    return text, dates


_EN_VOLUME_RE = re.compile(r"\bVol(?:ume|\.)?[ \t]*[-–]?[ \t]*(\d{1,2})\b")


def rule_protected_tokens(text: str, existing: Iterable[str] = ()) -> list[str]:
    """Return stable, non-overlapping immutable tokens in source order.

    Existing syntax placeholders (for example Markdown inline-code tokens) are
    retained.  Longest-first selection prevents a number inside a stationing
    expression from being registered twice.
    """
    candidates = [(match.start(), match.end(), match.group(0)) for match in _PROTECTED_RE.finditer(text)]
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    result = list(existing)
    occupied: list[tuple[int, int]] = [
        (match.start(), match.end())
        for token in result if token
        for match in re.finditer(re.escape(token), text)
    ]
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

    def bounded(token: str) -> str:
        # Whole tokens only, as validation counts them: the "2" of
        # "Gulberg 2" also matched inside "29th January", the model dropped
        # that marker, and the date came out as 1月9日.
        before = r"(?<![A-Za-z0-9])" if token[:1].isalnum() else ""
        after = r"(?![A-Za-z0-9])" if token[-1:].isalnum() else ""
        return before + re.escape(token) + after

    pattern = re.compile("|".join(bounded(token) for token in values))
    replacements: list[tuple[str, str]] = []

    def replace(match: re.Match[str]) -> str:
        marker = f"[[TRP_{len(replacements):04d}]]"
        replacements.append((marker, match.group(0)))
        return marker

    return ProtectedText(pattern.sub(replace, text), tuple(replacements))


def restore_after_translation(text: str, protected: ProtectedText) -> str:
    """Restore protected source values only when every placeholder survives."""
    restored = _unwrap_bracketed_values(text, protected)
    from collections import Counter

    # How many markers stand for each value ("6" in "Section 6 ... Page 6-78").
    occurrences = Counter(value for _, value in protected.replacements)
    for marker, value in protected.replacements:
        marker_count = restored.count(marker)
        if marker_count == 1:
            restored = restored.replace(marker, value)
            continue
        # Some translation engines emit an immutable literal unchanged
        # instead of echoing its marker. Accept that when the literal is
        # present as often as the source has it (once per marker for that
        # value; requiring exactly one failed every value used twice);
        # otherwise fail closed as before. The value counts themselves are
        # checked again by validation.
        if marker_count == 0 and 1 <= restored.count(value) <= occurrences[value]:
            continue
        raise ValueError(f"protected placeholder was not preserved: {marker}")
    return restored


def restore_markers_best_effort(text: str, protected: ProtectedText) -> str:
    """Put back every surviving placeholder after a strict restore failed.

    The result is still flagged for review by the caller, but a raw
    ``[[TRP_nnnn]]`` marker must never reach the output document.
    """
    text = _unwrap_bracketed_values(text, protected)
    for marker, value in protected.replacements:
        text = text.replace(marker, value)
    return text


def _unwrap_bracketed_values(text: str, protected: ProtectedText) -> str:
    """A model told "[[TRP_0000]] stands for 2024" sometimes writes "[[2024]]"
    (marker brackets around the value): that is the marker, not new brackets."""
    for marker, value in protected.replacements:
        if marker not in text:
            text = text.replace(f"[[{value}]]", marker)
    return text
