"""No Playwright process may root itself inside this checkout (#409).

Windows refuses to delete a directory that is a live process's current working
directory. Everything Playwright spawns inherits one — the Node driver from
pytest, each browser from the driver, each helper from the browser — so a suite
run from `whatsapp-radar-wt-<N>` used to leave that whole tree rooted in the
worktree. A `WebKitNetworkProcess` helper wedged inside termination then pinned
the emptied worktree directory permanently.

`conftest._neutral_driver_cwd` fixes the cause by starting the driver from
`%TEMP%`. This pins that arrangement, and it is deliberately an assertion about
*working directories*, not about leaks: a wedge is a load-sensitive teardown
race that cannot be reproduced on demand, but the rooting that turns one into a
permanently-pinned directory is deterministic and cheap to check while a browser
is live.

One test node asserts both facts: the driver is a `node.exe` no image-name sweep
would recognise, and the helpers are the processes that actually wedge.
Mirrors project-scaffolding's test of the same name (#236).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.e2e._browser_sweep import (
    HELPER_IMAGE_NAMES,
    _iter_process_table,
    _read_process_cwd,
    path_is_within,
)

# Derived here rather than imported from `conftest`: pytest loads conftest.py
# as a top-level `conftest` module, so importing it by package path would load
# a second copy of it.
REPO_ROOT = Path(__file__).resolve().parents[2]


def _driver_pid(playwright: Any) -> int | None:
    """The Node driver's pid, or None when Playwright's internals moved.

    Best-effort by design, like `_browser_sweep._read_process_cwd`: this reaches
    into private attributes, so it degrades to "unknown" and the test reports
    that rather than passing on a fact it never established. The sync API hands
    back a wrapper whose connection lives on `_impl_obj`; both spellings are
    tried rather than assuming which object arrived.
    """
    for root in (getattr(playwright, "_impl_obj", None), playwright):
        try:
            return int(root._connection._transport._proc.pid)
        except Exception:  # pragma: no cover - internals moved
            continue
    return None


def test_no_playwright_process_roots_in_this_checkout(page: Any, playwright: Any) -> None:
    """Neither the driver nor any browser helper may hold this checkout as its cwd."""
    page.goto("data:text/html,<h1>#409 helper cwd isolation</h1>")

    failures: list[str] = []

    driver_pid = _driver_pid(playwright)
    if driver_pid is None:
        pytest.skip(
            "driver pid unreachable (Playwright internals changed) -- "
            "reporting unknown rather than a false pass"
        )
    driver_cwd = _read_process_cwd(driver_pid)
    if driver_cwd is None:
        failures.append(
            f"driver#{driver_pid}: working directory unreadable -- unknown, "
            "not verified outside the checkout"
        )
    elif path_is_within(driver_cwd, REPO_ROOT):
        failures.append(
            f"driver#{driver_pid} runs from {driver_cwd}, inside this checkout; "
            "every browser and helper it spawns inherits that"
        )

    for pid, _ppid, name in _iter_process_table():
        if name not in HELPER_IMAGE_NAMES:
            continue
        cwd = _read_process_cwd(pid)
        if cwd and path_is_within(cwd, REPO_ROOT):
            failures.append(f"{name}#{pid} runs from {cwd}, inside this checkout")

    assert not failures, (
        "Playwright process(es) are rooted inside this checkout and will pin it "
        "against deletion once they wedge (#409):\n  " + "\n  ".join(failures)
    )
