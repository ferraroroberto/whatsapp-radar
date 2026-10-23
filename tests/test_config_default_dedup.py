"""Structural check for the #438 default-duplication class.

A ``src/config`` dataclass that re-types its own default as a literal fallback
inside ``parse()`` — instead of reading it off a ``defaults = XConfig()``
instance — lets the two copies drift silently if one is edited and not the
other (the #438 finding, already true of ``presence``/``transcription``/
``tripwire``/``gmail``/``task_os``/``hub``/``calendar`` before that issue).
This statically walks every ``src/config`` module and asserts that whenever a
dataclass field has its own literal (or ``Path(literal)``) default, the
module's ``*.get(name, ...)`` fallback for that field reads ``defaults.name``,
never a re-typed constant.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "src" / "config"
# Not one-dataclass-per-subsystem modules: __init__.py aggregates every
# submodule's own parse(), _shared.py has no dataclass at all. Both are a
# different shape from the per-subsystem convention #438 is about.
_SKIP = {"__init__.py", "_shared.py"}


def _scalar_default_fields(tree: ast.Module) -> set[str]:
    """Dataclass field names whose own default is a literal scalar or ``Path(literal)``.

    Collection-typed fields (``field(default_factory=...)``, tuple/list/dict
    literals) are excluded: an empty-collection fallback written as ``[]``
    instead of ``defaults.x`` is not the drift risk #438 is about, and
    flagging it would make this check reject the issue's own "good" examples
    (e.g. ``family.py``'s ``childcare_windows``).
    """
    fields: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for item in node.body:
            if not isinstance(item, ast.AnnAssign) or item.value is None:
                continue
            name = item.target.id if isinstance(item.target, ast.Name) else None
            if name is None:
                continue
            value = item.value
            if isinstance(value, ast.Constant):
                fields.add(name)
            elif (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "Path"
                and len(value.args) == 1
                and isinstance(value.args[0], ast.Constant)
            ):
                fields.add(name)
    return fields


def _get_call_violations(tree: ast.Module, scalar_fields: set[str]) -> list[tuple[str, int]]:
    """``raw.get(<field>, <fallback>)`` calls whose fallback isn't ``defaults.<field>``.

    Scoped to the ``raw`` parameter every module's ``parse(raw: dict[str, Any], ...)``
    takes — the one-default-per-subsystem knob the #438 finding is about — so a
    nested per-item builder looping over its own ``item.get(...)`` (e.g.
    ``family.py``'s ``ChildcareWindow`` list parser) is a different, out-of-scope
    shape and never flagged here.
    """
    violations: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and len(node.args) == 2
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "raw"
        ):
            continue
        key_node = node.args[0]
        if not (isinstance(key_node, ast.Constant) and isinstance(key_node.value, str)):
            continue
        field = key_node.value
        if field not in scalar_fields:
            continue
        fallback = node.args[1]
        if isinstance(fallback, ast.Attribute) and fallback.attr == field:
            continue
        violations.append((field, node.lineno))
    return violations


def _config_modules() -> list[Path]:
    return sorted(p for p in _CONFIG_DIR.glob("*.py") if p.name not in _SKIP)


@pytest.mark.parametrize("path", _config_modules(), ids=lambda p: p.name)
def test_no_duplicated_scalar_defaults(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    scalar_fields = _scalar_default_fields(tree)
    violations = _get_call_violations(tree, scalar_fields)
    assert not violations, (
        f"{path.name} re-types a dataclass default as a literal fallback instead of "
        f"reading it off `defaults.<field>` (#438): {violations}"
    )
