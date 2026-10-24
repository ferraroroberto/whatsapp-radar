r"""The gate's instruction-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.instruction_ratchet_gate

The vendored `scripts/instruction_ratchet.py` is never edited here. This wrapper maps
its exits onto the gate: only 0 passes. Exit 3 (git unusable, a file it cannot read, a
fenced block that never closes) means the instruction files were not measured, so it
fails as NOT CONFIRMED instead of reading as a pass or a skip. `verify-before-ship.ps1`
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

SCRIPT = REPO_ROOT / "scripts" / "instruction_ratchet.py"

_PASS_LABEL = "no instruction file gained a lint hit or passed its size cap"
_FAIL_LABELS = {
    EXIT_NEW: "a new instruction-file hit, an over-cap file, or the baseline is missing",
    EXIT_CONFIG: "bad [instruction_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- the instruction files could not be measured",
}


def run(script: Path = SCRIPT) -> int:
    return run_ratchet(script, "instruction ratchet", _PASS_LABEL, _FAIL_LABELS)


if __name__ == "__main__":
    sys.exit(run())
