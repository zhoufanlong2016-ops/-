"""Compatibility utilities for bounded PDF provider concurrency.

The former BabelDOC subprocess worker was removed.  This module remains only
for callers that imported the small runtime-limit helper.
"""

from __future__ import annotations

import os


def _pdf_runtime_limits() -> tuple[int, int]:
    def bounded(name: str, default: int) -> int:
        try:
            return max(1, min(8, int(os.environ.get(name, str(default)))))
        except (TypeError, ValueError):
            return default

    qps = bounded("DOCUMENT_TRANSLATOR_PDF_QPS", 6)
    workers = bounded("DOCUMENT_TRANSLATOR_PDF_WORKERS", 6)
    return qps, min(qps, workers)
