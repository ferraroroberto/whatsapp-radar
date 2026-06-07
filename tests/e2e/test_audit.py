"""Browser e2e: the Audit tab lists a run and drills into its per-chat trace.

Self-contained: fire a dry-run scan from the Run tab (stub classifier + fixture
connector, as in test_execution) to generate a real review_run + analysis_trace,
then switch to Audit, open the run, and assert the per-chat decision record
renders — the issue's "open a run, see the per-chat trace" acceptance, through
the UI. Drives only the sanitized e2e fixture DB.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.smoke
def test_audit_drilldown_shows_trace(page: Page, base_url: str) -> None:
    page.goto(base_url)

    # 1. Generate a run: dry-run the whole pipeline on the seeded fixture data.
    page.locator("#tabExecution").click()
    expect(page.locator("#paneExecution")).to_be_visible()
    page.locator("#execModeDry").click()
    expect(page.locator("#execDryOpts")).to_be_visible()
    page.locator("#execRunScan").click()
    expect(page.locator("#execViewerMeta")).to_contain_text("completed", timeout=45_000)

    # 2. Switch to Audit — the run shows up in the list.
    page.locator("#tabAudit").click()
    expect(page.locator("#paneAudit")).to_be_visible()
    runs = page.locator("#auditRuns .audit-run-li")
    expect(runs.first).to_be_visible(timeout=10_000)
    expect(runs.first).to_contain_text("Dry run")

    # 3. Drill in — the detail card opens with a funnel and at least one per-chat
    #    trace block carrying the seeded monitored chat's name.
    runs.first.click()
    expect(page.locator("#auditDetailCard")).to_be_visible(timeout=10_000)
    expect(page.locator("#auditFunnel")).to_contain_text("Stage 1")
    trace = page.locator("#auditTraces .audit-trace").first
    expect(trace).to_be_visible()
    expect(trace).to_contain_text("Class 4A Group")

    # 4. Expanding the trace reveals the input the decision was made on.
    trace.locator("summary").click()
    expect(trace).to_contain_text("Input messages")
