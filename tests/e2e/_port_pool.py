"""Keep the e2e harness off the host's loopback ephemeral-port pool.

Every closed loopback connection leaves an (ephemeral port, server port) pair in
TIME_WAIT for minutes, and Windows answers a ``connect()`` that lands on one with
``net::ERR_ADDRESS_IN_USE`` instead of trying another port. So the more
connections a run opens, the likelier the next page load fails on a page that is
fine, and a second gate on the same host adds its own.

Three pieces, reached from ``conftest.py``:

* ``pytest_runtest_makereport`` + ``pytest_terminal_summary`` — a failure whose text
  carries ``ERR_ADDRESS_IN_USE`` (or Playwright's Node-side ``connect ENOBUFS``)
  leads with ``PORT POOL EXHAUSTED`` and the run ends with a section of that name,
  so the gate output names a host-capacity problem rather than a page defect.
* ``reset_context`` — returns a shared browser context to a first-visit state, so
  consecutive tests reuse one context's keep-alive connections and HTTP cache
  instead of closing a context's sockets per test.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from typing import Any

import pytest
from playwright.sync_api import BrowserContext, Page

logger = logging.getLogger(__name__)

PORT_POOL_MARKERS = ("ERR_ADDRESS_IN_USE", "connect ENOBUFS")
PORT_POOL_HEADLINE = (
    "PORT POOL EXHAUSTED — the host ran out of loopback ephemeral ports or socket buffers "
    "(ERR_ADDRESS_IN_USE / connect ENOBUFS); this is not a page failure. Another gate or a "
    "recent run holds ports in TIME_WAIT: wait a few minutes and re-run instead of debugging "
    "the page."
)

_RESET_PAGE_STATE_JS = (
    "() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} }"
)


def is_port_pool_failure(text: str) -> bool:
    return any(marker in text for marker in PORT_POOL_MARKERS)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Relabel a report that failed on a port-pool marker so its first line names the host.

    Also records each phase's report on the item (``rep_setup`` / ``rep_call``), which the
    ``page`` fixture reads to retire a shared context after a failed test."""
    report = yield
    setattr(item, f"rep_{report.when}", report)
    if (
        report.failed
        and is_port_pool_failure(report.longreprtext)
        and PORT_POOL_HEADLINE not in report.longreprtext
    ):
        report.longrepr = f"{PORT_POOL_HEADLINE}\n\n{report.longreprtext}"
    return report


def pytest_terminal_summary(terminalreporter: Any) -> None:
    """List the tests that failed on the port pool in a section of their own."""
    hit = [
        report.nodeid
        for kind in ("failed", "error")
        for report in terminalreporter.stats.get(kind, [])
        if PORT_POOL_HEADLINE in report.longreprtext
    ]
    if not hit:
        return
    terminalreporter.write_sep("=", "PORT POOL EXHAUSTED", red=True, bold=True)
    terminalreporter.write_line(PORT_POOL_HEADLINE)
    for nodeid in dict.fromkeys(hit):
        terminalreporter.write_line(f"  {nodeid}")


def reset_context(context: BrowserContext, page: Page) -> None:
    """Return a shared context to a first-visit state for the next test.

    Clears everything a test leaves that the next one could read: the page's own
    storage and routes, and the context's cookies and permissions. A step that
    fails is logged and skipped, so one broken page cannot mask the test's own result.
    """
    steps = (
        lambda: page.evaluate(_RESET_PAGE_STATE_JS),
        lambda: page.unroute_all(behavior="ignore_errors"),
        context.clear_cookies,
        context.clear_permissions,
    )
    for step in steps:
        try:
            step()
        except Exception as exc:  # noqa: BLE001 — a reset step never masks the test's result
            logger.warning("⚠️ shared context reset step failed: %s", exc)
