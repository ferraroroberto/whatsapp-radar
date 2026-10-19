"""Browser e2e: Activity's run list and run sheet over route-mocked runs.

The coverage-gap marker, the Filtered-out fold and its drill-through, and the
family-check sections of the run sheet, each against a mocked payload (#373
merged the Audit tab into Activity). The live run-and-drill-in story is
test_execution.py's, which fires a real dry run.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._surface import surface


@pytest.mark.smoke
@pytest.mark.live_safe
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
    newest_failed = run(5, "2026-06-27T18:00:00+00:00", offline=False)
    newest_failed.update(status="failed", error="classifier contract error")
    payload = {
        "runs": [newest_failed, run(4, "2026-06-26T18:00:00+00:00", offline=False), *offline_runs],
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
    page.route("**/api/audit/runs?*", lambda route: route.fulfill(json=payload))

    page.goto(base_url)
    page.locator("#tabActivity").click()

    gap = page.locator("#activityList .audit-gap-li")
    expect(gap).to_have_count(1)
    expect(gap).to_contain_text("Coverage gap")
    expect(gap).to_contain_text("5 days · 3 scans offline")
    rows = page.locator("#activityList .run-row")
    expect(rows).to_have_count(2)
    # Exceptions only: a Failed chip in the danger tone, nothing on the OK run,
    # and the header names the most severe exception (#373).
    expect(rows.first.locator(".status-chip")).to_have_text("Failed")
    expect(rows.first.locator(".status-chip")).to_have_attribute("data-tone", "danger")
    expect(rows.nth(1).locator(".status-chip")).to_have_count(0)
    expect(page.locator("#activityStatus")).to_have_text("Last scan failed")
    expect(page.locator("#activityStatus")).to_have_attribute("data-tone", "danger")
    surface(page, "activity.coverage-gap")


@pytest.mark.smoke
@pytest.mark.live_safe
def test_audit_filtered_out_list_drills_into_run(page: Page, base_url: str) -> None:
    run_payload = {
        "runs": [],
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
    page.route("**/api/audit/runs?*", lambda route: route.fulfill(json=run_payload))
    page.route("**/api/audit/filtered?*", lambda route: route.fulfill(json=filtered_payload))
    page.route("**/api/audit/runs/7", lambda route: route.fulfill(json=detail_payload))

    page.goto(base_url)
    page.locator("#tabActivity").click()

    # Filtered out is a fold row at the end of Messages that opens a sheet (#373).
    expect(page.locator("#filteredSheet")).to_be_hidden()
    page.locator("#activityFilteredOpen").click()
    expect(page.locator("#filteredSheet")).to_be_visible()
    # The day range is the segmented control: 30 days is raised by default,
    # and a tap moves the raised segment.
    days = page.locator("#auditFilteredDays")
    expect(days.locator('[data-days="30"]')).to_have_attribute("aria-pressed", "true")
    days.locator('[data-days="7"]').click()
    expect(days.locator('[data-days="7"]')).to_have_attribute("aria-pressed", "true")
    expect(days.locator('[data-days="30"]')).to_have_attribute("aria-pressed", "false")
    row = page.locator("#auditFiltered .audit-filtered-row")
    expect(row).to_contain_text("School Parents Group")
    expect(row).to_contain_text("Routine pickup acknowledgement")
    surface(page, "activity.filtered-sheet")
    row.click()
    expect(page.locator("#filteredSheet")).to_be_hidden()
    expect(page.locator("#runSheetCard")).to_be_visible()
    expect(page.locator("#runSheetTitle")).to_contain_text("#7")


# ------------------------------------------- travel-block sweep record (#276)

# A calendar-scan whose payload carries a full travel-block sweep. Sanitized:
# `example.com` calendar ids and generic labels only. The point of the block
# under test is that none of those ids reach the DOM inside it.
_TRAVEL_SECTION = {
    "status": "ok",
    "dry_run": False,
    "horizon_start": "2026-08-21T12:00:00+00:00",
    "horizon_end": "2026-08-23T12:00:00+00:00",
    "routes_calls": 4,
    "counts": {"desired": 3, "adds": 1, "deletes": 1,
               "keeps": 1, "protected": 1, "failures": 1},
    "adds": [
        {
            "leg": "out", "person": "parent-a", "calendar_id": "parent-a@example.com",
            "source_event_id": "e1", "event": "Class 4A Group meeting",
            "origin": "home", "destination": "school",
            "start": "2026-08-21T12:00:00+00:00", "end": "2026-08-21T12:25:00+00:00",
            "minutes": 25, "hash": "abc",
        },
    ],
    "deletes": [
        {
            "reason": "source_event_gone", "calendar_id": "parent-a@example.com",
            "event_id": "b1", "source_event_id": "e0", "leg": "home",
            "start": "2026-08-21T15:00:00+00:00", "hash": "old", "schema_version": 1,
        },
    ],
    "protected": [
        {
            "reason": "leg_unpriced", "calendar_id": "parent-b@example.com",
            "event_id": "b2", "source_event_id": "e2", "leg": "out",
            "start": "2026-08-22T07:00:00+00:00", "hash": "keep", "schema_version": 1,
        },
    ],
    "failures": [
        {
            "leg": "out", "person": "parent-b", "calendar_id": "parent-b@example.com",
            "source_event_id": "e2", "event": "School Parents Group",
            "status": "unpriced", "reason": "routes_error", "detail": "Routes API 429",
        },
    ],
    "apply": {
        "status": "applied",
        "counts": {"inserted": 1, "deleted": 1, "kept": 1, "skipped": 0, "backups": 1},
        "failures": [],
        "write_capability": {"parent-a@example.com": "writable"},
    },
}


def _calendar_run(section: dict[str, object] | None) -> dict[str, object]:
    summary: dict[str, object] = {
        "kind": "calendar-scan", "status": "ok",
        "conflicts": [], "missing_locations": [], "decisions": [],
    }
    if section is not None:
        summary["travel_blocks"] = section
    return {
        "id": 9,
        "kind": "calendar-scan",
        "summary": summary,
        "mode": "live",
        "status": "completed",
        "params": None,
        "started_at": "2026-08-21T12:00:00+00:00",
        "completed_at": "2026-08-21T12:01:00+00:00",
        "notification_status": "none",
        "error": None,
        "sources": {},
        "funnel": {},
    }


def _open_travel_run(page: Page, base_url: str, section: dict[str, object] | None) -> None:
    run = _calendar_run(section)
    page.route(
        "**/api/audit/runs?*",
        lambda route: route.fulfill(json={"runs": [run], "coverage_gaps": []}),
    )
    page.route(
        "**/api/audit/filtered?*",
        lambda route: route.fulfill(
            json={"days": 30, "limit": 50, "offset": 0, "total": 0,
                  "has_more": False, "items": []}
        ),
    )
    page.route("**/api/audit/runs/9", lambda route: route.fulfill(json={"run": run, "traces": []}))
    page.goto(base_url)
    page.locator("#tabActivity").click()
    page.locator('#activityScope [data-scope="calendar"]').click()
    page.locator("#activityList .run-row .action-row-main").first.click()
    expect(page.locator("#runSheetCard")).to_be_visible()


@pytest.mark.smoke
@pytest.mark.live_safe
def test_audit_renders_the_travel_block_sweep_readably(page: Page, base_url: str) -> None:
    """The sweep record reads as legs and reasons, not as a JSON dump (#276)."""
    page.set_viewport_size({"width": 390, "height": 844})
    _open_travel_run(page, base_url, _TRAVEL_SECTION)

    block = page.locator("#runTraces .audit-travel")
    expect(block).to_have_count(1)
    expect(block).to_contain_text("live — blocks were written")
    expect(block).to_contain_text("3 leg(s) · 1 add · 1 delete · 1 kept · 1 left alone")

    # A planned block: person, leg kind, event title, time box and minutes.
    expect(block).to_contain_text("parent-a · out · \"Class 4A Group meeting\"")
    expect(block).to_contain_text("25 min")
    # A removal and a left-alone block, each with the reason that put it there —
    # and `left alone` is its own heading, never folded into `kept`.
    expect(block).to_contain_text("Travel blocks — planned removals (1)")
    expect(block).to_contain_text("source_event_gone")
    expect(block).to_contain_text("Travel blocks — left alone (1)")
    expect(block).to_contain_text("leg_unpriced")
    # An unpriced leg keeps its `unpriced` discriminator and its reason.
    expect(block).to_contain_text("unpriced (routes_error)")

    # The privacy line: no calendar id anywhere inside this block. The raw
    # payload dump below it is a separate, pre-existing surface.
    assert "@" not in (block.inner_text() or "")

    # It sits above the raw dump, which stays as the last resort.
    order = page.evaluate(
        """
        () => {
          const kids = [...document.querySelector('#runTraces').children];
          const travel = kids.findIndex((el) => el.classList.contains('audit-travel'));
          const dump = kids.findIndex(
            (el) => el.querySelector('.audit-field-title')?.textContent === 'Run payload');
          return [travel, dump];
        }
        """
    )
    assert order[0] >= 0 and order[1] > order[0], order

    # Funnel cells carry the headline counts too, at phone width, without
    # pushing the pane sideways.
    expect(page.locator("#runFunnel")).to_contain_text("Block adds")
    overflowing = page.evaluate(
        """
        () => {
          const vw = window.innerWidth;
          const bad = [];
          document.querySelectorAll('#runSheetCard *').forEach((el) => {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.right > vw + 0.5 && getComputedStyle(el).overflowX !== 'auto') {
              bad.push(String(el.className));
            }
          });
          return bad;
        }
        """
    )
    assert overflowing == [], f"travel-block audit section overflows 390px: {overflowing}"
    surface(page, "activity.travel-sweep")


@pytest.mark.smoke
@pytest.mark.live_safe
def test_audit_gated_sweep_is_named_not_zeroed(page: Page, base_url: str) -> None:
    """A gated sweep names its gate instead of rendering a plan of zeros."""
    _open_travel_run(page, base_url, {"status": "disabled"})
    block = page.locator("#runTraces .audit-travel")
    expect(block).to_contain_text("Gated: travel blocks are off")
    expect(block).to_contain_text("not a sweep that looked and found nothing to do")
    expect(block).not_to_contain_text("0 add")
    expect(page.locator("#runFunnel")).to_contain_text("Blocks")
    expect(page.locator("#runFunnel")).not_to_contain_text("Block adds")
    surface(page, "activity.travel-gated")


@pytest.mark.smoke
@pytest.mark.live_safe
def test_activity_skipped_line_counts_only_the_current_scope(
    page: Page, base_url: str
) -> None:
    """#393: traffic's cadence self-skips must not read as hidden Calendar runs."""
    page.route(
        "**/api/execution/runs*",
        lambda route: route.fulfill(
            json={"active": None, "runs": [], "skipped_count": 7,
                  "skipped_by_kind": {"traffic-check": 5, "calendar-scan": 2}}
        ),
    )
    page.route(
        "**/api/audit/runs?*",
        lambda route: route.fulfill(json={"runs": [], "coverage_gaps": []}),
    )
    page.route(
        "**/api/audit/filtered?*",
        lambda route: route.fulfill(
            json={"days": 30, "limit": 50, "offset": 0, "total": 0,
                  "has_more": False, "items": []}
        ),
    )
    page.goto(base_url)
    page.locator("#tabActivity").click()

    skipped = page.locator("#activitySkipped")
    page.locator('#activityScope [data-scope="calendar"]').click()
    expect(skipped).to_have_text("2 self-skipped runs hidden")
    page.locator('#activityScope [data-scope="traffic"]').click()
    expect(skipped).to_have_text("5 self-skipped runs hidden")
    surface(page, "activity.skipped-line")
    page.locator('#activityScope [data-scope="messages"]').click()
    expect(skipped).to_be_hidden()
