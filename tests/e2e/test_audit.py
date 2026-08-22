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
@pytest.mark.live_safe
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


# Not @pytest.mark.live_safe (issue #225): clicks #execRunScan, firing the
# real backend pipeline unmocked — under WR_E2E_LIVE this would run whatever
# connector/classifier the live tray is actually configured with, not the
# stub/fixture env autoboot injects. Stays out of live mode until mocked.
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
        "**/api/audit/runs",
        lambda route: route.fulfill(json={"runs": [run], "syncs": [], "coverage_gaps": []}),
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
    page.locator("#tabAudit").click()
    page.locator("#auditRuns .audit-run-li").first.click()
    expect(page.locator("#auditDetailCard")).to_be_visible()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
@pytest.mark.smoke
@pytest.mark.live_safe
def test_audit_renders_the_travel_block_sweep_readably(
    page: Page, base_url: str, color_scheme: str
) -> None:
    """The sweep record reads as legs and reasons, not as a JSON dump (#276)."""
    page.emulate_media(color_scheme=color_scheme)
    page.set_viewport_size({"width": 390, "height": 844})
    _open_travel_run(page, base_url, _TRAVEL_SECTION)

    block = page.locator("#auditTraces .audit-travel")
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
          const kids = [...document.querySelector('#auditTraces').children];
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
    expect(page.locator("#auditFunnel")).to_contain_text("Block adds")
    overflowing = page.evaluate(
        """
        () => {
          const vw = window.innerWidth;
          const bad = [];
          document.querySelectorAll('#auditDetailCard *').forEach((el) => {
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


@pytest.mark.smoke
@pytest.mark.live_safe
def test_audit_gated_sweep_is_named_not_zeroed(page: Page, base_url: str) -> None:
    """A gated sweep names its gate instead of rendering a plan of zeros."""
    _open_travel_run(page, base_url, {"status": "disabled"})
    block = page.locator("#auditTraces .audit-travel")
    expect(block).to_contain_text("Gated: travel blocks are off")
    expect(block).to_contain_text("not a sweep that looked and found nothing to do")
    expect(block).not_to_contain_text("0 add")
    expect(page.locator("#auditFunnel")).to_contain_text("Blocks")
    expect(page.locator("#auditFunnel")).not_to_contain_text("Block adds")
