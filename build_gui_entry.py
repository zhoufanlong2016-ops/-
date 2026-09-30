"""PyInstaller entry point for the double-clickable GUI executable.

Dispatches to the CLI when invoked with arguments (the GUI itself launches
the frozen exe as a subprocess this way, see document_translator.gui) and
falls back to the Tkinter GUI when launched with no arguments (a normal
double-click).
"""

from __future__ import annotations

import multiprocessing
import sys

# Required by PyInstaller for anything that uses multiprocessing.Pool /
# ProcessPoolExecutor (docvortex's own PDF render pool does): a frozen exe
# re-launches itself as the child process, and without this guard that
# relaunch re-enters this same entry point and recurses instead of running
# the actual worker bootstrap multiprocessing needs. Must run before any
# other import that might itself spin up a pool at import time.
multiprocessing.freeze_support()

from document_translator.__main__ import main

if len(sys.argv) > 1:
    raise SystemExit(main(sys.argv[1:]))

from document_translator.gui import run_gui

raise SystemExit(run_gui())
