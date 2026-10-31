"""The family-check run kinds are defined once, in `src.db.runs.FAMILY_KINDS`.

Seven hand-typed copies of the pair used to sit across the CLI, the run modal and
the Audit/Family routers; a third family check missed in any one silently dropped
out of that surface. This walks `app/` and `src/` and fails on any tuple, list or
set literal that spells out both kinds outside the one definition.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFINITION = REPO_ROOT / "src" / "db" / "runs.py"
_KINDS = {"calendar-scan", "traffic-check"}


def _hand_listed(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            literals = {
                el.value
                for el in node.elts
                if isinstance(el, ast.Constant) and isinstance(el.value, str)
            }
            if _KINDS <= literals:
                lines.append(node.lineno)
    return lines


def test_no_module_hand_lists_both_family_kinds() -> None:
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{line}"
        for root in ("app", "src")
        for path in sorted((REPO_ROOT / root).rglob("*.py"))
        if path != _DEFINITION
        for line in _hand_listed(path)
    ]
    assert not offenders, f"import FAMILY_KINDS from src.db.runs instead of re-listing: {offenders}"
