r"""The gate's docs-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.docs_ratchet_gate

`scripts/docs_ratchet.py` (vendored, never edited here) exits 0 on a pass, 1 when
the README grew or a doc gained a history marker, 2 on a bad table or baseline
and 3 when it could not confirm (git unusable, a non-UTF-8 file, an unclosed
fence). Exit 3 means nothing was checked, so it must fail the gate as its own
state and never read as a pass or a skip. The mapping is the one the static
ratchet wrapper owns; `verify-before-ship.ps1` calls this wrapper, never the
script.

Exit 0 only when the script exited 0; every other outcome exits 1 with its
reason on the last line.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.static_ratchet_gate import (
    EXIT_CONFIG,
    EXIT_NEW,
    EXIT_NOT_CONFIRMED,
    EXIT_PASS,
    REPO_ROOT,
    run_ratchet,
)

SCRIPT = REPO_ROOT / "scripts" / "docs_ratchet.py"

_PASS_LABEL = "no README growth and no new history marker"
_FAIL_LABELS = {
    EXIT_NEW: "README grew or a doc gained a history marker, or the baseline is missing",
    EXIT_CONFIG: "bad [docs_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- the docs could not be scanned, so nothing was checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == EXIT_PASS:
        return True, _PASS_LABEL
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    return run_ratchet(script, "docs ratchet", _PASS_LABEL, _FAIL_LABELS)


if __name__ == "__main__":
    sys.exit(run())
