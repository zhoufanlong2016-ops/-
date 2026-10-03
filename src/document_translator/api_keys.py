"""API keys come from one local file that is never committed.

``keys\api_keys.env`` next to the program (the exe's folder when packaged,
the project folder otherwise) holds DASHSCOPE_API_KEY (通义千问),
DEEPSEEK_API_KEY and OPENAI_API_KEY. Keys already set in the environment
win; a project ``.env`` is still read as a fallback.
"""

from __future__ import annotations

import sys
from pathlib import Path

KEY_NAMES = ("DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY")
KEYS_FILE = Path("keys") / "api_keys.env"


def key_file_candidates() -> list[Path]:
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(__file__).resolve().parents[2])
    roots.append(Path.cwd())
    seen: list[Path] = []
    for root in roots:
        candidate = root / KEYS_FILE
        if candidate not in seen:
            seen.append(candidate)
    return seen


def load_api_keys() -> Path | None:
    """Load the first keys file found; returns its path, or None."""
    try:
        from dotenv import find_dotenv, load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv is a declared dependency
        return None
    loaded = None
    for candidate in key_file_candidates():
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            loaded = candidate
            break
    load_dotenv(find_dotenv(usecwd=True), override=False)
    return loaded
