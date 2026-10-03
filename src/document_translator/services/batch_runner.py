"""Shared provider driver: cache first, each distinct text once, batches in parallel.

Every format used to hand its whole unit list to provider.translate_batch(),
which sends the packed batches one after another (175 s for a 117-cell
workbook), and none but PDF read a cache. Results are identical: the same
batches go to the same provider, only concurrently, and a unit's translation
depends only on its text and protected tokens (which is also the cache key).
"""

from __future__ import annotations

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Sequence

from document_translator.core import TranslationResult, TranslationUnit, sha256_text, validate_result_for_unit
from document_translator.providers.batch_limits import split_semantic_batches

_INTEGRITY_ERRORS = {"UNIT_ID_MISMATCH", "SOURCE_HASH_MISMATCH", "RESULT_HASH_MISMATCH"}
# Below this many units a batch is not worth splitting further.
_MIN_SPLIT_UNITS = 4


def worker_count() -> int:
    try:
        return max(1, min(8, int(os.environ.get("DOCUMENT_TRANSLATOR_PDF_WORKERS", "6"))))
    except ValueError:
        return 6


def plan_batches(units: Sequence[TranslationUnit], model: str, explicit_limit: int, workers: int) -> list[list[TranslationUnit]]:
    """Pack to the model's request budget, then halve the biggest batches
    until every worker has one: a short document otherwise is one long,
    sequential request."""
    batches = [list(batch) for batch in split_semantic_batches(list(units), model=model, explicit_limit=explicit_limit, overhead=64) if batch]
    while len(batches) < workers:
        largest = max(batches, key=len, default=[])
        if len(largest) < _MIN_SPLIT_UNITS:
            break
        index = batches.index(largest)
        half = len(largest) // 2
        batches[index : index + 1] = [largest[:half], largest[half:]]
    return batches


def _identity(provider: Any) -> dict[str, str]:
    return {
        "provider": str(getattr(provider, "provider_name", "")),
        "model": str(getattr(getattr(provider, "config", None), "model", "")),
        "prompt_version": str(getattr(provider, "prompt_version", "")),
        "glossary_version": str(getattr(provider, "glossary_version", "")),
    }


def translate_units(
    provider: Any,
    units: Sequence[TranslationUnit],
    *,
    cache: Any | None = None,
    translation_mode: str = "default",
    progress: Callable[[str], None] | None = None,
) -> list[TranslationResult]:
    """One result per unit, in order. Results are not validated here beyond
    what the cache requires; callers keep their own validation."""
    if not units:
        return []
    groups: dict[tuple[str, tuple[str, ...]], list[TranslationUnit]] = {}
    for unit in units:
        groups.setdefault((unit.source_text, tuple(unit.protected_tokens)), []).append(unit)
    representatives = [members[0] for members in groups.values()]

    identity = _identity(provider)
    results: dict[str, TranslationResult] = {}
    pending: list[TranslationUnit] = []
    for unit in representatives:
        cached = None
        if cache is not None:
            try:
                cached = cache.get(unit, translation_mode=translation_mode, **identity)
            except Exception:  # a stale or corrupt entry is simply re-translated
                cached = None
        if cached is not None:
            results[unit.id] = cached
        else:
            pending.append(unit)

    if pending:
        config = getattr(provider, "config", None)
        batches = plan_batches(
            pending, identity["model"], int(getattr(config, "batch_input_characters", 0) or 0), worker_count(),
        )
        if progress is not None:
            progress(
                f"translation: {len(representatives)} distinct texts ({len(units)} total), "
                f"{len(representatives) - len(pending)} from cache, {len(batches)} requests"
            )
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, min(worker_count(), len(batches)))) as pool:
            for batch, batch_results in zip(batches, pool.map(lambda batch: list(provider.translate_batch(batch)), batches)):
                if len(batch_results) != len(batch):
                    raise ValueError("batch translation count mismatch")
                # Providers answer by stable ID, not necessarily in order.
                by_id = {result.unit_id: result for result in batch_results if isinstance(result, TranslationResult)}
                if set(by_id) != {unit.id for unit in batch}:
                    raise ValueError("batch translation ID mismatch")
                done += len(batch)
                # Read by the GUI to show "已翻译 x/y".
                print(f"translation: {done}/{len(pending)}", file=sys.stderr, flush=True)
                for unit in batch:
                    result = by_id[unit.id]
                    results[unit.id] = result
                    # sqlite connections stay on this thread: store after the batch returns.
                    if cache is not None and result.validation_status == "valid" and not validate_result_for_unit(unit, result):
                        try:
                            cache.put(unit, result, translation_mode=translation_mode)
                        except Exception:
                            pass

    ordered: list[TranslationResult] = []
    for unit in units:
        result = results[groups[(unit.source_text, tuple(unit.protected_tokens))][0].id]
        if result.unit_id != unit.id:
            result = result.model_copy(update={"unit_id": unit.id, "source_hash": sha256_text(unit.source_text)})
        ordered.append(result)
    return ordered


def settle(
    provider: Any,
    units: Sequence[TranslationUnit],
    results: Sequence[TranslationResult],
    *,
    normalize: Callable[[str], str] | None = None,
) -> tuple[list[TranslationResult], list[dict[str, object]]]:
    """Keep the document going when one unit fails validation.

    A failing unit gets one more request on its own; if it still fails, its
    best translation is kept (its source text if the translation is blank)
    and reported, instead of discarding a whole document after minutes of
    work (a PPT failed after 4 minutes over one doubled "FIDIC").
    """

    def prepared(result: TranslationResult) -> TranslationResult:
        if normalize is None:
            return result
        text = normalize(result.translation)
        return result if text == result.translation else result.model_copy(update={"translation": text, "result_hash": sha256_text(text)})

    settled: list[TranslationResult] = []
    warnings: list[dict[str, object]] = []
    for unit, result in zip(units, results, strict=True):
        result = prepared(result)
        errors = validate_result_for_unit(unit, result)
        if errors:
            try:
                retry = prepared(list(provider.translate_batch([unit]))[0])
                retry_errors = validate_result_for_unit(unit, retry)
                if len(retry_errors) < len(errors):
                    result, errors = retry, retry_errors
            except Exception:
                pass
        integrity = [error for error in errors if error.endswith("_MISMATCH") and error.split(":")[0] in _INTEGRITY_ERRORS]
        if integrity:
            # A result that does not belong to this unit is never written.
            raise ValueError("invalid batch translation: " + "; ".join(integrity))
        if errors:
            if not result.translation.strip():
                result = result.model_copy(update={"translation": unit.source_text, "result_hash": sha256_text(unit.source_text)})
            warnings.append({"object_id": unit.location.object_id, "errors": errors})
        settled.append(result)
    return settled, warnings


def _with_local_dates(unit: TranslationUnit) -> TranslationUnit:
    """The unit as the model should see it: full dates already in the target
    form ("29th January 2026" -> "2026年1月29日"), as the PDF path does.

    With the day and year as opaque placeholders a model mixed them up
    ("2026年29月"); a date has one correct rendering, so it is not left to it.
    """
    from document_translator.core import generate_unit_id
    from document_translator.translation_rules import localize_chinese_dates, rule_protected_tokens

    text, dates = localize_chinese_dates(unit.source_text, unit.source_language, unit.target_language)
    if not dates:
        return unit
    kept = [token for token in unit.protected_tokens if token in text]
    data = unit.model_dump(exclude={"id"})
    data.update(source_text=text, protected_tokens=rule_protected_tokens(text, [*kept, *dates]))
    return TranslationUnit(id=generate_unit_id(**{k: data[k] for k in (
        "document_hash", "format", "location", "source_language", "target_language", "source_text",
        "protected_tokens", "style_signature", "context_before", "context_after",
    ) if k in data}), **data)


def translate_and_settle(
    provider: Any,
    units: Sequence[TranslationUnit],
    *,
    cache: Any | None = None,
    normalize: Callable[[str], str] | None = None,
) -> tuple[list[TranslationResult], list[dict[str, object]]]:
    """translate_units() + settle(), with dates localised before the model."""
    prepared = [_with_local_dates(unit) for unit in units]
    raw = translate_units(provider, prepared, cache=cache)
    settled, warnings = settle(provider, prepared, raw, normalize=normalize)
    rebound = [
        result if model is unit else result.model_copy(update={"unit_id": unit.id, "source_hash": sha256_text(unit.source_text)})
        for unit, model, result in zip(units, prepared, settled, strict=True)
    ]
    return rebound, warnings
