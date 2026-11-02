r"""The gate's runtime-write-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.runtime_write_ratchet_gate

The vendored `scripts/runtime_write_ratchet.py` is never edited here. This wrapper maps its
exits onto the gate: only 0 passes. Exit 3 (a write target it could not resolve, git
unusable, a tracked Python file that cannot be read or does not parse) means the tree was
not fully checked, so it fails as NOT CONFIRMED instead of reading as a pass or a skip.
`verify-before-ship.ps1` calls the wrapper for the static check, never the script; the
`--snapshot` / `--probe` bracket around the test stages calls the script directly. The shared
mapping lives in `static_ratchet_gate`.

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

SCRIPT = REPO_ROOT / "scripts" / "runtime_write_ratchet.py"

_PASS_LABEL = "no write target under a code folder beyond the baseline"
_FAIL_LABELS = {
    EXIT_NEW: "a new write target under a code folder, or the baseline is missing",
    EXIT_CONFIG: "bad [runtime_write_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: (
        "NOT CONFIRMED -- a write target could not be resolved, or the tree could not be read; "
        "nothing was fully checked"
    ),
}


def run(script: Path = SCRIPT) -> int:
    return run_ratchet(script, "runtime write ratchet", _PASS_LABEL, _FAIL_LABELS)


if __name__ == "__main__":
    sys.exit(run())
