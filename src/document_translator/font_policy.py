"""Shared Office font policy for translated CJK text."""

from __future__ import annotations

import re

CJK_FONT = "SimHei"
LATIN_FONT = "Arial"
LATIN_NARROW_FONT = "Arial Narrow"

# CJK ideographs, plus the CJK punctuation (\u3000-\u303f: "\u3002", "\uff0c", "\u3001",
# the fullwidth brackets, ...) and fullwidth/halfwidth forms (\uff00-\uffef:
# fullwidth digits, punctuation and Latin letters, halfwidth katakana) a
# translation commonly mixes in with them. A cell whose translated text is
# just a bare number followed by a Chinese-style full stop -- "1\u3002" instead
# of "1." -- previously fell through this check into the Latin-only font
# path used for content this regex judged CJK-free: ArialMT has no glyph
# for U+3002, and PyMuPDF renders the resulting missing glyph as a visible
# blank box with the wrong ToUnicode entry rather than failing loudly
# (confirmed directly: the exact same text renders correctly once the
# CJK-capable fallback font, which does have this glyph, is chosen for
# it instead).
_CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


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


# NOTE: an earlier version of this module protected a short embedded Latin/
# digit phrase (a standard code like "NFPA 2001", a proper noun like
# "Gulshan e Ravi") from PyMuPDF's own CJK-mixed line wrap by replacing its
# internal spaces with U+00A0 (NBSP). An isolated single-cell probe showed
# clean rendering with no corruption, but the REAL multi-cell table render
# (render_table_translations() + subset_fonts() + repair_pdf_text_cmaps())
# reproduced the exact same failure this project already hit and reverted
# once before for a different character (the bullet •, see
# pdf_table.py's _normalise_render_text()): the glyph draws as a visible
# blank box while repair_pdf_text_cmaps() maps its ToUnicode back to a
# harmless U+0020, hiding the corruption from text EXTRACTION but not from
# the reader's eyes -- confirmed directly against the real 10-page
# pipeline, not just an isolated probe. Removed again for the same reason:
# never trade a cosmetic line-wrap fix for a visible corrupted glyph.
# Fixing the underlying line break correctly needs PyMuPDF's own word-wrap
# replaced with a purpose-built layout pass that treats such a phrase as
# one atomic token when measuring lines -- a larger, separate piece of work.
