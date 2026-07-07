"""Browser e2e: the Execution tab runs a dry-run scan and shows the funnel.

Drives the real PWA against the autobooted webapp: switch to Run, pick Dry-run,
fire the full pipeline, and assert a run completes with its funnel + live output
rendered — the issue's "dry-run shows the funnel" acceptance, through the UI.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.smoke
def test_execution_dry_run_shows_funnel(
    page: Page, base_url: str, scaled: Callable[[float], int]
) -> None:
    page.goto(base_url)
    page.locator("#tabExecution").click()
    expect(page.locator("#paneExecution")).to_be_visible()

    # Pick dry-run, then run the whole pipeline.
    page.locator("#execModeDry").click()
    expect(page.locator("#execDryOpts")).to_be_visible()
    page.locator("#execRunScan").click()

    # The viewer appears and the run reaches a terminal state (stub classifier →
    # a couple of seconds; allow generous headroom for the cold subprocess).
    viewer = page.locator("#execViewer")
    expect(viewer).to_be_visible(timeout=scaled(15_000))
    expect(page.locator("#execViewerMeta")).to_contain_text(
        "completed", timeout=scaled(45_000)
    )

    # Funnel strip rendered, and the live output captured the dry-run banner.
    expect(page.locator("#execFunnel")).to_contain_text("Notify")
    expect(page.locator("#execOutput")).to_contain_text("dry_run")

    # The run shows up in the recent-runs list too.
    expect(page.locator("#execRuns")).to_contain_text("Full pipeline")


@pytest.mark.smoke
def test_execution_offline_shows_reconnect(
    page: Page, base_url: str, scaled: Callable[[float], int]
) -> None:
    """With an empty sidecar buffer the health pill offers a relaunch (#29).

    Asserts the offline affordance *renders* — it never clicks Reconnect, so no
    Node process is ever spawned from the test.
    """
    page.goto(base_url)
    page.locator("#tabExecution").click()
    expect(page.locator("#paneExecution")).to_be_visible()

    # The throwaway buffer (conftest) has no status.json → 'stopped' → a red
    # "Offline" status word and a visible Reconnect button.
    expect(page.locator("#execReconnect")).to_be_visible(timeout=scaled(10_000))
    expect(page.locator("#execReconnectBtn")).to_be_visible()
    expect(page.locator("#execHealthStatus")).to_have_text("Offline")
