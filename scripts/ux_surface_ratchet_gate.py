r"""Gate stage for the UX surface ratchet.

    & .\.venv\Scripts\python.exe -m scripts.ux_surface_ratchet_gate

Runs the vendored `scripts/ux_surface_ratchet.py`, which is never edited here, and turns its
exit code into the gate's verdict. Only 0 passes; exit 3 means a file could not be read or JS
never closed, so the tree was not fully checked and the stage reports NOT CONFIRMED.
"""

from __future__ import annotations

import sys
from pathlib import Path

from scripts import static_ratchet_gate as shared

SCRIPT = shared.REPO_ROOT / "scripts" / "ux_surface_ratchet.py"

_FAIL_LABELS = {
    shared.EXIT_NEW: "a surface rule rose, or the map, its page or the baseline is missing",
    shared.EXIT_CONFIG: "bad [ux_surface_ratchet] table, baseline or arguments",
    shared.EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- a file could not be read or closed",
}


def run(script: Path = SCRIPT) -> int:
    return shared.run_ratchet(
        script, "ux surface ratchet", "every surface is marked, mapped and current", _FAIL_LABELS
    )


if __name__ == "__main__":
    sys.exit(run())
