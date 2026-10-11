"""Tokens the providers' APIs report as used, totalled for this process.

One CLI process translates one document (the GUI starts one per file), so
the totals are that document's cost: printed at the end of the command and
written into a PDF's report.
"""

from __future__ import annotations

import threading
from typing import Any

_LOCK = threading.Lock()
_TOTALS: dict[str, int] = {}


def record(**counts: Any) -> None:
    """Add one response's counts; missing or non-numeric values count as 0."""
    with _LOCK:
        _TOTALS["requests"] = _TOTALS.get("requests", 0) + 1
        for name, value in counts.items():
            if isinstance(value, int) and value > 0:
                _TOTALS[name] = _TOTALS.get(name, 0) + value


def record_response(body: Any) -> Any:
    """Record the ``usage`` of one API reply and return the reply unchanged.

    Claude reports input without the cached part; OpenAI-style replies
    (DeepSeek, Qwen, OpenAI) count the cached part inside the input, so it is
    taken out to make both mean the same.
    """
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return body
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        details = usage.get("prompt_tokens_details") or {}
        cached = usage.get("prompt_cache_hit_tokens", details.get("cached_tokens", 0) if isinstance(details, dict) else 0)
        cached = cached if isinstance(cached, int) else 0
        prompt = usage.get("prompt_tokens") if isinstance(usage.get("prompt_tokens"), int) else 0
        record(input_tokens=prompt - cached, cache_read_input_tokens=cached, output_tokens=usage.get("completion_tokens"))
    else:
        details = usage.get("input_tokens_details") or {}
        cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
        cached = cached if isinstance(cached, int) else 0
        prompt = usage.get("input_tokens") if isinstance(usage.get("input_tokens"), int) else 0
        record(
            input_tokens=prompt - cached,
            cache_read_input_tokens=usage.get("cache_read_input_tokens", cached),
            cache_creation_input_tokens=usage.get("cache_creation_input_tokens"),
            output_tokens=usage.get("output_tokens"),
        )
    return body


def totals() -> dict[str, int]:
    with _LOCK:
        return dict(_TOTALS)


def reset() -> None:
    with _LOCK:
        _TOTALS.clear()


def summary() -> str | None:
    """"tokens: input 9,012 (cache read 0), output 1,034, 6 requests", or None."""
    counts = totals()
    if not counts:
        return None
    text = f"tokens: input {counts.get('input_tokens', 0):,}"
    cached = counts.get("cache_read_input_tokens", 0) + counts.get("cache_creation_input_tokens", 0)
    if cached:
        text += f" (cache read {counts.get('cache_read_input_tokens', 0):,}, cache write {counts.get('cache_creation_input_tokens', 0):,})"
    return text + f", output {counts.get('output_tokens', 0):,}, {counts['requests']} requests"
