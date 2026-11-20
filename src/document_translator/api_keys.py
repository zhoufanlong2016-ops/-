"""API keys come from one local file that is never committed.

``keys\api_keys.env`` in the project folder holds DASHSCOPE_API_KEY (通义千问),
DEEPSEEK_API_KEY, OPENAI_API_KEY and ANTHROPIC_API_KEY (Claude). The
packaged exe (``dist\<app>\``) reads the same file two folders up, so the
source and every build share one copy; a ``keys`` folder next to the exe
is only a fallback (an exe copied to another computer). Keys already set
in the environment win; a project ``.env`` is still read as a fallback.
"""

from __future__ import annotations

import sys
from pathlib import Path

KEY_NAMES = ("DASHSCOPE_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY")
KEYS_FILE = Path("keys") / "api_keys.env"


def key_file_candidates() -> list[Path]:
    roots = []
    if getattr(sys, "frozen", False):
        exe_folder = Path(sys.executable).resolve().parent
        # dist\<app>\ -> the project folder first: kept beside the exe, the
        # keys were copied into every build and its backups, and drifted apart
        roots.extend(list(exe_folder.parents)[:2][::-1])
        roots.append(exe_folder)
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
