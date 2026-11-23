r"""Gate stage for the windows-only ratchet: run the vendored script, label its exit code.

    & .\.venv\Scripts\python.exe -m scripts.windows_only_ratchet_gate

The vendored `scripts/windows_only_ratchet.py` is never edited here. This wrapper maps its exits
onto the gate: only 0 passes. Exit 3 (git unusable, a test module that cannot be read or parsed)
means the tests were not fully checked, so it fails as NOT CONFIRMED instead of reading as a pass
or a skip. The shared mapping lives in `static_ratchet_gate`.
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

SCRIPT = REPO_ROOT / "scripts" / "windows_only_ratchet.py"

_PASS_LABEL = "every test that needs Windows carries the windows_only marker"
_FAIL_LABELS = {
    EXIT_NEW: "a test that needs Windows lacks the windows_only marker, or the baseline is missing",
    EXIT_CONFIG: "bad [windows_only_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: (
        "NOT CONFIRMED -- a test module could not be read or parsed, so nothing was fully checked"
    ),
}


def run(script: Path = SCRIPT) -> int:
    return run_ratchet(script, "windows only ratchet", _PASS_LABEL, _FAIL_LABELS)


if __name__ == "__main__":
    sys.exit(run())
