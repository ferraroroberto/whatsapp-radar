"""A long wait in runtime code is a named constant, never a bare number.

A `timeout=` of 10 s or more bounds something unattended (a boot-time cert check, a
CLI call from the tray); a reader must be able to tell a deliberate budget from a
guess and know whether it may change. Naming it gives it a place for that reason.
Shorter literals are exempt: a 1-5 s bound is self-evident at the call site.
"""

from __future__ import annotations

import ast
from pathlib import Path

from src.paths import PROJECT_ROOT

RUNTIME_DIRS = ("src", "app")
LONG_TIMEOUT_SECONDS = 10


def _bare_long_timeouts(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        kw.value.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for kw in node.keywords
        if kw.arg == "timeout"
        and isinstance(kw.value, ast.Constant)
        and isinstance(kw.value.value, int | float)
        and kw.value.value >= LONG_TIMEOUT_SECONDS
    ]


def test_runtime_code_has_no_bare_long_timeouts() -> None:
    offenders = [
        f"{path.relative_to(PROJECT_ROOT).as_posix()}:{line}"
        for directory in RUNTIME_DIRS
        for path in sorted((PROJECT_ROOT / directory).rglob("*.py"))
        for line in _bare_long_timeouts(path)
    ]
    assert not offenders, (
        f"name these timeout= literals (>= {LONG_TIMEOUT_SECONDS} s) as constants "
        f"with a one-clause reason: {offenders}"
    )
