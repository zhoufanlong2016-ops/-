"""Provider-aware semantic batch limits.

The limits are deliberately below advertised context windows.  They reserve
space for the policy, glossary, stable IDs, and the translated JSON envelope;
they are request safety limits, not claims about a provider's maximum context.
"""
from __future__ import annotations


def model_batch_characters(model: str, explicit: int = 0) -> int:
    if explicit:
        return explicit
    name = (model or "").casefold()
    if name.startswith("qwen"):
        # Covers both the current qwen3.7-plus/qwen3.8-flash/qwen3.8-max
        # lineup and older bare qwen-plus/qwen-max names; "flash" trades
        # context safety margin for speed/cost, "max" gets the most room.
        if "flash" in name:
            return 4500
        if "max" in name:
            return 6000
        return 5000
    if name.startswith("gpt-5.6-sol"):
        return 10000
    if name.startswith("gpt-5.6-terra"):
        return 8000
    if name.startswith("gpt-5.6-luna"):
        return 6000
    if name.startswith("gpt-") or name.startswith("o"):
        return 6000
    return 4096


def split_semantic_batches(units, *, model: str, explicit_limit: int = 0, overhead: int = 96):
    """Pack complete semantic units without splitting a unit or retrying it."""
    limit = model_batch_characters(model, explicit_limit)
    batches = [[]]
    size = 0
    for unit in units:
        cost = len(unit.source_text) + overhead
        if batches[-1] and size + cost > limit:
            batches.append([])
            size = 0
        batches[-1].append(unit)
        size += cost
    return batches
