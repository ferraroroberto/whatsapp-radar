r"""The gate's comment-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.comment_ratchet_gate

The vendored `scripts/comment_ratchet.py` is never edited here. This wrapper maps
its exits onto the gate: only 0 passes. Exit 3 (git unusable, or a file the
scanner cannot read) means no count was established, so it fails as NOT CONFIRMED
instead of reading as a pass or a skip. `verify-before-ship.ps1` calls the
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

SCRIPT = REPO_ROOT / "scripts" / "comment_ratchet.py"

_PASS_LABEL = "no file carries more issue references in comments than its baseline"
_FAIL_LABELS = {
    EXIT_NEW: "a file gained an issue reference in a comment, or the baseline is missing",
    EXIT_CONFIG: "bad [comment_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- the comments could not be scanned; nothing was checked",
}


def run(script: Path = SCRIPT) -> int:
    return run_ratchet(script, "comment ratchet", _PASS_LABEL, _FAIL_LABELS)


if __name__ == "__main__":
    sys.exit(run())
