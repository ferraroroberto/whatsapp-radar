r"""Gate stage for the request budget ratchet: run the vendored script, label its exit code.

    & .\.venv\Scripts\python.exe -m scripts.request_budget_ratchet_gate

The vendored `scripts/request_budget_ratchet.py` (with its reader `scripts/request_budget_scan.py`)
is never edited here. This wrapper maps its exits onto the gate: only 0 passes. Exit 3 (git
unusable, a file that cannot be read or closed, Python that does not parse) means the tree was
not fully checked, so it fails as NOT CONFIRMED instead of reading as a pass or a skip. The
shared mapping lives in `static_ratchet_gate`.
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

SCRIPT = REPO_ROOT / "scripts" / "request_budget_ratchet.py"

_PASS_LABEL = "no new poll, fast loop, default-pool submit or missing timing middleware"
_FAIL_LABELS = {
    EXIT_NEW: "a rule's hits rose above the baseline, or the baseline is missing",
    EXIT_CONFIG: "bad [request_budget_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: (
        "NOT CONFIRMED -- a file could not be read or parsed, so nothing was fully checked"
    ),
}


def run(script: Path = SCRIPT) -> int:
    return run_ratchet(script, "request budget ratchet", _PASS_LABEL, _FAIL_LABELS)


if __name__ == "__main__":
    sys.exit(run())
