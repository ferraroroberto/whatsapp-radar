"""The ``src.db.store`` facade must not keep a function alive that production never calls.

The dead-code ratchet counts a name in ``store.__all__`` as referenced, so a function
that only the facade and the tests touch is invisible to it. This check re-derives the
reference count without the facade: every callable the facade exports is used by a
production module, or used inside the module that defines it.
"""

from __future__ import annotations

import ast
from pathlib import Path

from src.db import store

ROOT = Path(__file__).resolve().parent.parent
PRODUCTION_FOLDERS = ("src", "app", "scripts")
FACADE = ROOT / "src" / "db" / "store.py"


def _referenced_names() -> set[str]:
    """Names used as a value or attribute anywhere in production code except the facade."""
    names: set[str] = set()
    for folder in PRODUCTION_FOLDERS:
        for path in (ROOT / folder).rglob("*.py"):
            if path == FACADE:
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Name):
                    names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    names.add(node.attr)
    return names


def test_every_callable_the_facade_exports_has_a_production_caller() -> None:
    referenced = _referenced_names()
    orphans = sorted(
        name
        for name in store.__all__
        if callable(getattr(store, name)) and name not in referenced
    )
    assert not orphans, (
        f"exported by src.db.store but never used by src/, app/ or scripts/: {orphans}. "
        "Delete the function, or move a test-only helper under tests/."
    )
