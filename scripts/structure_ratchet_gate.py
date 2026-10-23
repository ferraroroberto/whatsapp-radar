r"""The gate's structure-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.structure_ratchet_gate

The vendored `scripts/structure_ratchet.py` is never edited here. This wrapper maps
its exits onto the gate: only 0 passes. Exit 3 (git unusable, a file it cannot read
or parse, an unknown `shape`) means the layout was not established, so it fails as
NOT CONFIRMED instead of reading as a pass or a skip. `verify-before-ship.ps1`
calls the wrapper, never the script; the shared mapping lives in `static_ratchet_gate`.

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

SCRIPT = REPO_ROOT / "scripts" / "structure_ratchet.py"

_PASS_LABEL = "no offender is new or above its baseline, and the layout matches the shape"
_FAIL_LABELS = {
    EXIT_NEW: "a new or grown offender, an undeclared root folder, or the baseline is missing",
    EXIT_CONFIG: "bad [structure_ratchet] table, baseline, shape or arguments",
    EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- the layout could not be measured; nothing was checked",
}


def run(script: Path = SCRIPT) -> int:
    return run_ratchet(script, "structure ratchet", _PASS_LABEL, _FAIL_LABELS)


if __name__ == "__main__":
    sys.exit(run())
