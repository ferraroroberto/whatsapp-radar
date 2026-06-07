"""Browser e2e: the Execution tab runs a dry-run scan and shows the funnel.

Drives the real PWA against the autobooted webapp: switch to Run, pick Dry-run,
fire the full pipeline, and assert a run completes with its funnel + live output
rendered — the issue's "dry-run shows the funnel" acceptance, through the UI.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.smoke
def test_execution_dry_run_shows_funnel(page: Page, base_url: str) -> None:
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
    expect(viewer).to_be_visible(timeout=15_000)
    expect(page.locator("#execViewerMeta")).to_contain_text("completed", timeout=45_000)

    # Funnel strip rendered, and the live output captured the dry-run banner.
    expect(page.locator("#execFunnel")).to_contain_text("Notify")
    expect(page.locator("#execOutput")).to_contain_text("dry_run")

    # The run shows up in the recent-runs list too.
    expect(page.locator("#execRuns")).to_contain_text("Full pipeline")
