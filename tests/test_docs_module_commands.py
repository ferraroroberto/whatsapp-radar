"""A `python -m <module>` command quoted in docs must still run something.

A doc step pointed `scripts.traffic_smoke` readers at `src.traffic.routes_client`
(#440): the module imports cleanly and exits 0, but it has no `__main__` guard,
so running it the way the doc said printed nothing instead of the promised
one-line verdict. Nothing in the gate caught that -- pytest/ruff/mypy never read
docs/, and the module itself is perfectly valid library code.

This scans `README.md` and `docs/**/*.md` for every `-m <dotted.module>`
invocation, resolves it to a project-tree file (skipping third-party tools like
`pip`/`pytest`/`ruff`/`mypy` that never resolve to one), and asserts that file
has an `if __name__ == "__main__"` guard -- the structural property a doc
command actually needs. It would have failed against the pre-fix
calendar-bootstrap.md, which pointed step 9 at `src.traffic.routes_client`.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_MODULE_COMMAND_RE = re.compile(r"-m\s+([A-Za-z_][A-Za-z0-9_.]*)")


def _doc_module_commands() -> list[tuple[Path, str]]:
    doc_files = [REPO_ROOT / "README.md", *sorted((REPO_ROOT / "docs").glob("**/*.md"))]
    found: list[tuple[Path, str]] = []
    for doc in doc_files:
        text = doc.read_text(encoding="utf-8")
        for match in _MODULE_COMMAND_RE.finditer(text):
            found.append((doc, match.group(1)))
    return found


def _module_file(module: str) -> Path | None:
    candidate = REPO_ROOT / Path(*module.split("."))
    package_init = candidate / "__init__.py"
    module_file = candidate.with_suffix(".py")
    if module_file.is_file():
        return module_file
    if package_init.is_file():
        return package_init
    return None


def test_doc_commands_point_at_modules_with_a_main_guard() -> None:
    commands = _doc_module_commands()
    assert commands, "expected at least one `-m <module>` command in docs/README"

    missing_guard: list[str] = []
    for doc, module in commands:
        resolved = _module_file(module)
        if resolved is None:
            continue  # not a project module (pip/pytest/ruff/mypy/...): nothing to check
        source = resolved.read_text(encoding="utf-8")
        if "__main__" not in source:
            doc_rel = doc.relative_to(REPO_ROOT)
            module_rel = resolved.relative_to(REPO_ROOT)
            missing_guard.append(f"{doc_rel} -> -m {module} ({module_rel} has no __main__ guard)")

    header = "doc command(s) point at a module with no __main__ guard:"
    message = header + "\n" + "\n".join(missing_guard)
    assert not missing_guard, message
