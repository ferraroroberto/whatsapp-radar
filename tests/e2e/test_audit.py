"""Browser e2e: the Audit tab lists a run and drills into its per-chat trace.

Self-contained: fire a dry-run scan from the Run tab (stub classifier + fixture
connector, as in test_execution) to generate a real review_run + analysis_trace,
then switch to Audit, open the run, and assert the per-chat decision record
renders — the issue's "open a run, see the per-chat trace" acceptance, through
the UI. Drives only the sanitized e2e fixture DB.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.smoke
def test_audit_collapses_offline_window_into_one_gap_marker(
    page: Page, base_url: str
) -> None:
    def run(run_id: int, started_at: str, *, offline: bool) -> dict[str, object]:
        return {
            "id": run_id,
            "kind": "scan",
            "summary": None,
            "mode": "live",
            "status": "failed" if offline else "completed",
            "params": None,
            "started_at": started_at,
            "completed_at": started_at,
            "notification_status": "offline" if offline else "none",
            "error": "connector offline" if offline else None,
            "sources": {},
            "funnel": {},
        }

    offline_runs = [
        run(3, "2026-06-25T18:00:00+00:00", offline=True),
        run(2, "2026-06-21T18:00:00+00:00", offline=True),
        run(1, "2026-06-20T18:00:00+00:00", offline=True),
    ]
    payload = {
        "runs": [run(4, "2026-06-26T18:00:00+00:00", offline=False), *offline_runs],
        "syncs": [],
        "coverage_gaps": [
            {
                "started_at": "2026-06-20T18:00:00+00:00",
                "ended_at": "2026-06-25T18:00:00+00:00",
                "duration_days": 5,
                "failed_runs": 3,
                "run_ids": [1, 2, 3],
                "recovered_at": "2026-06-26T18:00:00+00:00",
                "recovery_run_id": 4,
            }
        ],
    }
    page.route("**/api/audit/runs", lambda route: route.fulfill(json=payload))

    page.goto(base_url)
    page.locator("#tabAudit").click()

    gap = page.locator("#auditRuns .audit-gap-li")
    expect(gap).to_have_count(1)
    expect(gap).to_contain_text("Coverage gap")
    expect(gap).to_contain_text("5 days · 3 scans offline")
    expect(page.locator("#auditRuns .audit-run-li")).to_have_count(1)


@pytest.mark.smoke
def test_audit_filtered_out_list_drills_into_run(page: Page, base_url: str) -> None:
    run_payload = {
        "runs": [],
        "syncs": [],
        "coverage_gaps": [],
    }
    filtered_payload = {
        "days": 30,
        "limit": 50,
        "offset": 0,
        "total": 1,
        "has_more": False,
        "items": [
            {
                "trace_id": 11,
                "run_id": 7,
                "created_at": "2026-07-18T18:00:00+00:00",
                "source": "whatsapp",
                "display_name": "School Parents Group",
                "stage1_passed": True,
                "stage1_roots": ["pickup"],
                "llm_called": True,
                "parsed_result": {
                    "action_required": False,
                    "priority": "low",
                    "summary": "Routine pickup acknowledgement",
                },
                "final_action": "not_actionable",
            }
        ],
    }
    detail_payload = {
        "run": {
            "id": 7,
            "kind": "scan",
            "summary": None,
            "mode": "live",
            "status": "completed",
            "params": None,
            "started_at": "2026-07-18T18:00:00+00:00",
            "completed_at": "2026-07-18T18:01:00+00:00",
            "notification_status": "none",
            "error": None,
            "sources": {},
            "funnel": {},
        },
        "traces": [],
    }
    page.route("**/api/audit/runs", lambda route: route.fulfill(json=run_payload))
    page.route("**/api/audit/filtered?*", lambda route: route.fulfill(json=filtered_payload))
    page.route("**/api/audit/runs/7", lambda route: route.fulfill(json=detail_payload))

    page.goto(base_url)
    page.locator("#tabAudit").click()

    filtered = page.locator("#auditFilteredCard")
    expect(filtered).not_to_have_attribute("open", "")
    filtered.locator("summary").click()
    row = page.locator("#auditFiltered .audit-filtered-row")
    expect(row).to_contain_text("School Parents Group")
    expect(row).to_contain_text("Routine pickup acknowledgement")
    row.click()
    expect(page.locator("#auditDetailCard")).to_be_visible()
    expect(page.locator("#auditDetailTitle")).to_contain_text("#7")


@pytest.mark.smoke
def test_audit_drilldown_shows_trace(
    page: Page, base_url: str, scaled: Callable[[float], int]
) -> None:
    page.goto(base_url)

    # 1. Generate a run: dry-run the whole pipeline on the seeded fixture data.
    page.locator("#tabExecution").click()
    expect(page.locator("#paneExecution")).to_be_visible()
    page.locator("#execModeDry").click()
    expect(page.locator("#execDryOpts")).to_be_visible()
    page.locator("#execRunScan").click()
    expect(page.locator("#execViewerMeta")).to_contain_text(
        "completed", timeout=scaled(45_000)
    )

    # 2. Switch to Audit — the run shows up in the list.
    page.locator("#tabAudit").click()
    expect(page.locator("#paneAudit")).to_be_visible()
    runs = page.locator("#auditRuns .audit-run-li")
    expect(runs.first).to_be_visible(timeout=scaled(10_000))
    expect(runs.first).to_contain_text("Dry run")

    # 3. Drill in — the detail card opens with a funnel and at least one per-chat
    #    trace block carrying the seeded monitored chat's name.
    runs.first.click()
    expect(page.locator("#auditDetailCard")).to_be_visible(timeout=scaled(10_000))
    expect(page.locator("#auditFunnel")).to_contain_text("Stage 1")
    trace = page.locator("#auditTraces .audit-trace").first
    expect(trace).to_be_visible()
    expect(trace).to_contain_text("Class 4A Group")

    # 4. Expanding the trace reveals the per-message breakdown (#12): each message
    #    with its own Stage-1 / LLM verdict badge, so the operator can see which
    #    messages triggered and which didn't — no black box.
    trace.locator("summary").click()
    expect(trace).to_contain_text("WhatsApp · Stage 1")
    expect(trace).to_contain_text("Messages (")
    messages = trace.locator(".audit-msg")
    expect(messages.first).to_be_visible()
    expect(messages.first.locator(".audit-msg-badge").first).to_be_visible()
