"""Shared Office font policy for translated CJK text."""

from __future__ import annotations

import re

CJK_FONT = "SimHei"
LATIN_FONT = "Arial"
LATIN_NARROW_FONT = "Arial Narrow"

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def contains_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text))


def latin_font_for(text: str) -> str:
    """Return the baseline Latin face.

    Width-sensitive selection is intentionally deferred to the PowerPoint
    layout pass, which can measure the rendered text in its real text frame.
    A character-count threshold is not reliable: a short phrase can overflow
    a narrow box while a much longer phrase can fit in a wide one.
    """
    return LATIN_FONT
