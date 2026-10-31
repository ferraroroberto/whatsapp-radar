"""Every ratchet gate `verify-before-ship.ps1` runs is listed in the docs agents follow.

`CLAUDE.md` and `README.md` each carry the gate command list; an agent that follows one
alone skips any gate missing from it. The script is the single source of truth, so this
fails when either doc omits a `scripts.<name>_ratchet_gate` the script calls.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_RE = re.compile(r"scripts\.([a-z_]+_ratchet_gate)")


def _gates(path: Path) -> set[str]:
    return set(_GATE_RE.findall(path.read_text(encoding="utf-8")))


def test_script_runs_at_least_one_gate() -> None:
    assert _gates(REPO_ROOT / "scripts" / "verify-before-ship.ps1")


@pytest.mark.parametrize("doc", ["CLAUDE.md", "README.md"])
def test_doc_lists_every_gate_the_script_runs(doc: str) -> None:
    missing = _gates(REPO_ROOT / "scripts" / "verify-before-ship.ps1") - _gates(REPO_ROOT / doc)
    assert not missing, f"{doc} omits gate(s) verify-before-ship.ps1 runs: {sorted(missing)}"
