r"""The gate's dead-code-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.dead_code_ratchet_gate

The vendored `scripts/dead_code_ratchet.py` is never edited here. This wrapper maps its
exits onto the gate: only 0 passes. Exit 3 (git unusable, a tracked Python file that
cannot be read or does not parse) means the definitions were never matched against their
uses, so it fails as NOT CONFIRMED instead of reading as a pass or a skip.
`verify-before-ship.ps1` calls the wrapper, never the script; the shared mapping lives in
`static_ratchet_gate`.

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

SCRIPT = REPO_ROOT / "scripts" / "dead_code_ratchet.py"

_PASS_LABEL = "no function, class or method nothing refers to beyond the baseline"
_FAIL_LABELS = {
    EXIT_NEW: "a new definition nothing refers to, or the baseline is missing",
    EXIT_CONFIG: "bad [dead_code_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- a Python file could not be parsed; nothing was checked",
}


def run(script: Path = SCRIPT) -> int:
    return run_ratchet(script, "dead code ratchet", _PASS_LABEL, _FAIL_LABELS)


if __name__ == "__main__":
    sys.exit(run())
