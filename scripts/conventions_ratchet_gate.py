r"""The gate's conventions-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.conventions_ratchet_gate

The vendored `scripts/conventions_ratchet.py` is never edited here. This wrapper maps its
exits onto the gate: only 0 passes. Exit 3 (git unusable, a tracked Python file that
cannot be read or does not parse) means the tree was not checked, so it fails as NOT
CONFIRMED instead of reading as a pass or a skip. `verify-before-ship.ps1` calls the
wrapper, never the script; the shared mapping lives in `static_ratchet_gate`.

A failure prints its reason on the last line and exits 1.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.static_ratchet_gate import (
    EXIT_CONFIG,
    EXIT_NEW,
    EXIT_NOT_CONFIRMED,
    REPO_ROOT,
    run_ratchet,
)

SCRIPT = REPO_ROOT / "scripts" / "conventions_ratchet.py"

_PASS_LABEL = "no convention hit beyond the baseline"
_FAIL_LABELS = {
    EXIT_NEW: "a new convention hit, or the baseline is missing",
    EXIT_CONFIG: "bad [conventions_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- a Python file could not be parsed; nothing was checked",
}


def run(script: Path = SCRIPT) -> int:
    return run_ratchet(script, "conventions ratchet", _PASS_LABEL, _FAIL_LABELS)


if __name__ == "__main__":
    sys.exit(run())
