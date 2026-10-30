"""A bare `except Exception:` must not return the same value a genuine
established outcome would (#490).

`scripts/gen_tailscale_cert.py` folded "the cert could not be parsed" into
`_expiring_within`'s own `False` ("not expiring") and `_tailscale_hostname_from_cert`'s
own `None` ("not a Tailscale cert") -- an unreadable cert then silently stopped
auto-renewal, and the only symptom was an expired HTTPS cert discovered on the
phone. Global `CLAUDE.md`'s "Verify before declaring done" names this exact
shape: "Any check, gate, health probe, or classifier that can fail to
establish a fact must report that as its own state ... never folded into the
passing state."

A handler whose causes all legitimately collapse to the same outcome (no
distinct fact is being hidden) can mark itself with a
``# no-silent-except-exempt: <reason>`` comment anywhere in its body, mirroring
this repo's `# <rule>-exempt:` convention for `[conventions_ratchet]`.

This walks every function under `src/`, `app/` and `scripts/` for a `try` whose
`except Exception`/`except BaseException`/bare `except` body is *only* a
`return` of a literal (``True``/``False``/``None``/a number/a string) with no
logging or print call first -- the shape that hides a parse/read failure
inside a value a caller cannot tell apart from an established fact. A legitimate
"nothing to do" `except` still needs a breadcrumb (log/print) before it returns,
so this never fires on those.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("src", "app", "scripts")

_BREADCRUMB_CALLS = {"print", "warning", "error", "info", "debug", "critical", "exception"}


def _is_breadcrumb_call(node: ast.stmt) -> bool:
    if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
        return False
    func = node.value.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    return name in _BREADCRUMB_CALLS


def _is_bare_sentinel_return(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.Return):
        return False
    value = stmt.value
    if value is None:
        return True  # `return` with no value is `None`, same hidden-sentinel shape
    return isinstance(value, ast.Constant)


def _is_exempted(lines: list[str], handler: ast.ExceptHandler) -> bool:
    end = max((getattr(s, "end_lineno", s.lineno) for s in handler.body), default=handler.lineno)
    span = lines[handler.lineno - 1 : end]
    return any("no-silent-except-exempt" in line for line in span)


def _offending_handlers(tree: ast.AST, lines: list[str]) -> list[int]:
    offending: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            is_broad = handler.type is None or (
                isinstance(handler.type, ast.Name)
                and handler.type.id in ("Exception", "BaseException")
            )
            if not is_broad or not handler.body:
                continue
            if any(_is_breadcrumb_call(s) for s in handler.body):
                continue  # a logged/printed reason is the required breadcrumb -- not silent
            if _is_exempted(lines, handler):
                continue
            if len(handler.body) == 1 and _is_bare_sentinel_return(handler.body[0]):
                offending.append(handler.lineno)
    return offending


def _python_files() -> list[Path]:
    files: list[Path] = []
    for top in SCAN_DIRS:
        root = REPO_ROOT / top
        if root.is_dir():
            files.extend(sorted(root.rglob("*.py")))
    return files


def test_no_silent_except_returns_an_indistinguishable_sentinel() -> None:
    offenders: list[str] = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text, filename=str(path))
        for lineno in _offending_handlers(tree, text.splitlines()):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "a broad `except` returns a bare literal with no breadcrumb, folding an "
        "unestablished fact into a passing-looking state (global CLAUDE.md, "
        "'Verify before declaring done'): " + ", ".join(offenders)
    )
