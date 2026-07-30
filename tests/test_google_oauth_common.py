"""Portability contract for the shared OAuth-bootstrap package.

``google_oauth_common`` is copied byte-for-byte alongside ``gmail_readonly``,
``calendar_readonly``, or ``calendar_write`` when one of them is lifted into
another repo (docs/gmail-reuse.md). It must stay free of imports from this
repo's application code and must not reach back into any of the three
concrete clients it serves — the dependency direction is one-way.
"""

from __future__ import annotations

import ast
from pathlib import Path


def test_component_imports_no_application_or_client_modules() -> None:
    component_root = Path(__file__).parents[1] / "google_oauth_common"
    imported_modules: set[str] = set()
    for path in component_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
    forbidden_prefixes = (
        "src",
        "app",
        "scripts",
        "gmail_readonly",
        "calendar_readonly",
        "calendar_write",
    )
    assert not any(
        module == prefix or module.startswith(f"{prefix}.")
        for module in imported_modules
        for prefix in forbidden_prefixes
    )
