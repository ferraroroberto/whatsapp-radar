r"""Gate stage for the usage track ratchet: run the vendored script, label its exit code.

    & .\.venv\Scripts\python.exe -m scripts.usage_track_ratchet_gate

The vendored `scripts/usage_track_ratchet.py` (with its reader `scripts/usage_track_scan.py`)
is never edited here. This wrapper maps its exits onto the gate: only 0 passes. Exit 3 (a file
that cannot be read, or JS that never closes) means the tree was not fully checked, so it fails
as NOT CONFIRMED instead of reading as a pass or a skip. The shared mapping lives in
`static_ratchet_gate`.
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

SCRIPT = REPO_ROOT / "scripts" / "usage_track_ratchet.py"

_PASS_LABEL = "no untracked control, sheet or built id beyond the baseline"
_FAIL_LABELS = {
    EXIT_NEW: (
        "a rule's hits rose above the baseline, the app does not reach its usage log, "
        "or the baseline is missing"
    ),
    EXIT_CONFIG: "bad [usage_track_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: (
        "NOT CONFIRMED -- a file could not be read or closed, so nothing was fully checked"
    ),
}


def run(script: Path = SCRIPT) -> int:
    return run_ratchet(script, "usage track ratchet", _PASS_LABEL, _FAIL_LABELS)


if __name__ == "__main__":
    sys.exit(run())
