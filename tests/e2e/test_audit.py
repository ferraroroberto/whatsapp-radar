"""Browser e2e: Activity's run list and a calendar run's sheet over route-mocked runs.

Two flows, each against mocked payloads (no DB seeding, no run fired, no Google
call; hence ``live_safe``): the list (coverage-gap marker, Filtered-out fold and
its drill-through, the skipped-runs line) and a calendar-scan run's sheet (the
travel-block sweep read as legs and reasons, a gated sweep named rather than
zeroed, and the raw payload dump withholding calendar ids and addresses). The
live run-and-drill-in story is test_execution.py's, which fires a real dry run.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._audit_runs import open_audit_activity, reset_page
from tests.e2e._surface import surface


def _gap_marker_phase(page: Page, base_url: str) -> None:
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


def _filtered_out_phase(page: Page, base_url: str) -> None:
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


def _skipped_line_phase(page: Page, base_url: str) -> None:
    """Traffic's cadence self-skips must not read as hidden Calendar runs."""
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


@pytest.mark.smoke
@pytest.mark.live_safe
def test_activity_list_gap_marker_filtered_out_drill_and_skipped_line(
    page: Page, base_url: str
) -> None:
    _gap_marker_phase(page, base_url)
    reset_page(page)
    _filtered_out_phase(page, base_url)
    reset_page(page)
    _skipped_line_phase(page, base_url)


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


def _open_calendar_run(page: Page, base_url: str, run: dict[str, Any]) -> None:
    open_audit_activity(page, base_url, run, "calendar")
    page.locator("#activityList .run-row .action-row-main").first.click()
    expect(page.locator("#runSheetCard")).to_be_visible()


def _open_travel_run(page: Page, base_url: str, section: dict[str, object] | None) -> None:
    _open_calendar_run(page, base_url, _calendar_run(section))


def _live_sweep_phase(page: Page, base_url: str) -> None:
    """The sweep record reads as legs and reasons, not as a JSON dump."""
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


def _gated_sweep_phase(page: Page, base_url: str) -> None:
    """A gated sweep names its gate instead of rendering a plan of zeros."""
    _open_travel_run(page, base_url, {"status": "disabled"})
    block = page.locator("#runTraces .audit-travel")
    expect(block).to_contain_text("Gated: travel blocks are off")
    expect(block).to_contain_text("not a sweep that looked and found nothing to do")
    expect(block).not_to_contain_text("0 add")
    expect(page.locator("#runFunnel")).to_contain_text("Blocks")
    expect(page.locator("#runFunnel")).not_to_contain_text("Block adds")
    surface(page, "activity.travel-gated")


#: One token per sensitive field, so a leak names the field that leaked rather
#: than just proving that something did.
CALENDAR_ID = "SENTINELCALENDARID@leak.invalid"
ORIGIN = "SENTINELORIGINSTREET 1"
DESTINATION = "SENTINELDESTINATIONSTREET 2"
RAW_LOCATION = "SENTINELRAWLOCATIONSTREET 3"
ORIGIN_LABEL = "SENTINELORIGINLABELSTREET 4"
BLOCK_LOCATION = "SENTINELBLOCKLOCATIONSTREET 5"
LATLNG = "SENTINELLATLNG"

SENTINELS = [
    CALENDAR_ID, ORIGIN, DESTINATION, RAW_LOCATION,
    ORIGIN_LABEL, BLOCK_LOCATION, LATLNG,
]


def _summary() -> dict[str, Any]:
    """A calendar-scan payload with a sentinel in every sensitive field.

    Shaped like the real thing — a decision trace, a travel-block section with
    adds / deletes / protected / failures, and a traffic-style checked leg — so
    the redaction is exercised at every depth the payload actually reaches.
    """
    return {
        "kind": "calendar-scan",
        "status": "ok",
        "conflicts": [],
        "missing_locations": [
            {"person": "parent-a", "event": "Class 4A Group meeting",
             "start": "2026-08-20T09:00:00+00:00"},
        ],
        "decisions": [
            {"person": "parent-a", "event": "Class 4A Group meeting",
             "start": "2026-08-20T09:00:00+00:00", "end": "2026-08-20T10:00:00+00:00",
             "raw_location": RAW_LOCATION, "kind": "away", "source": "location",
             "assumed": False, "commute": True, "video_link": False},
        ],
        "checked": [
            {"person": "parent-a", "event": "Class 4A Group meeting", "status": "ok",
             "origin": ORIGIN, "destination": DESTINATION,
             "origin_label": ORIGIN_LABEL, "origin_latlng": [LATLNG, LATLNG],
             "eta_min": 22, "delay_min": 3, "normal_min": 19,
             "anchor": "event_start", "dedup_key": "parent-a::class 4a group meeting"},
        ],
        "summary": {"status": "sent", "text": "Everything is fine"},
        "travel_blocks": {
            "status": "ok",
            "dry_run": True,
            "routes_calls": 3,
            "horizon_start": "2026-08-20T00:00:00+00:00",
            "horizon_end": "2026-08-22T00:00:00+00:00",
            "counts": {"desired": 2, "adds": 1, "deletes": 1,
                       "keeps": 0, "protected": 1, "failures": 1},
            "adds": [
                {"leg": "out", "person": "parent-a", "calendar_id": CALENDAR_ID,
                 "source_event_id": "e1", "event": "Class 4A Group meeting",
                 "origin": ORIGIN, "destination": DESTINATION,
                 "start": "2026-08-20T08:35:00+00:00", "end": "2026-08-20T09:00:00+00:00",
                 "minutes": 25, "hash": "abc123"},
            ],
            "deletes": [
                {"reason": "orphaned", "calendar_id": CALENDAR_ID, "event_id": "blk-1",
                 "source_event_id": "e2", "leg": "home",
                 "start": "2026-08-20T17:00:00+00:00", "hash": "def456",
                 "schema_version": 1, "location": BLOCK_LOCATION},
            ],
            "protected": [
                {"reason": "leg_unpriced", "calendar_id": CALENDAR_ID, "event_id": "blk-2",
                 "source_event_id": "e3", "leg": "out",
                 "start": "2026-08-21T07:30:00+00:00", "hash": "ghi789",
                 "schema_version": 1},
            ],
            "failures": [
                {"leg": "out", "person": "parent-b", "calendar_id": CALENDAR_ID,
                 "source_event_id": "e4", "event": "School Parents Group",
                 "status": "unpriced", "reason": "routes_error",
                 "detail": "Routes API returned HTTP 429"},
            ],
            "apply": {
                "status": "dry_run",
                "counts": {"inserted": 0, "deleted": 0, "kept": 0,
                           "skipped": 2, "backups": 0},
                "failures": [
                    {"operation": "delete", "reason": "no_write_token",
                     "calendar_id": CALENDAR_ID, "detail": None},
                ],
                "write_capability": {CALENDAR_ID: "writable"},
            },
        },
    }


def _run() -> dict[str, Any]:
    return {
        "id": 9,
        "kind": "calendar-scan",
        "summary": _summary(),
        "mode": "dry_run",
        "status": "completed",
        "params": None,
        "started_at": "2026-08-20T07:00:00+00:00",
        "completed_at": "2026-08-20T07:00:20+00:00",
        "notification_status": "none",
        "error": None,
        "sources": {},
        "funnel": {},
    }


def _sentinels_stay_out_of_the_dom_but_the_dump_stays_diagnostic(page: Page) -> dict[str, Any]:
    # The dump renders after the sheet opens; a negative check taken before it
    # exists would pass whatever the dump held.
    dump = page.locator(".audit-field", has_text="Run payload").locator("pre")
    expect(dump).not_to_be_empty()
    # None of it is on the page: checked against the rendered text and the
    # serialised HTML, since an attribute or a hidden node leaks just as well.
    body_text = page.locator("body").inner_text()
    body_html = page.content()
    for sentinel in SENTINELS:
        assert sentinel not in body_text, f"{sentinel} rendered as text"
        assert sentinel not in body_html, f"{sentinel} present in the DOM"

    # Redact, do not remove: a dump an empty payload could satisfy is gutted.
    parsed: dict[str, Any] = json.loads(dump.inner_text())
    assert parsed["status"] == "ok"
    assert parsed["travel_blocks"]["counts"] == {
        "desired": 2, "adds": 1, "deletes": 1, "keeps": 0, "protected": 1, "failures": 1
    }
    assert parsed["travel_blocks"]["routes_calls"] == 3
    assert parsed["travel_blocks"]["deletes"][0]["reason"] == "orphaned"
    assert parsed["travel_blocks"]["failures"][0]["detail"] == "Routes API returned HTTP 429"
    assert parsed["travel_blocks"]["apply"]["failures"][0]["reason"] == "no_write_token"
    assert parsed["checked"][0]["eta_min"] == 22
    assert parsed["decisions"][0]["source"] == "location"
    assert parsed["kind"] == "calendar-scan"
    assert parsed["travel_blocks"]["horizon_start"] == "2026-08-20T00:00:00+00:00"
    return parsed


def _withheld_fields_are_marked(page: Page, parsed: dict[str, Any]) -> None:
    # A withheld field keeps its key and loses only its value.
    adds = parsed["travel_blocks"]["adds"][0]
    assert "calendar_id" in adds
    assert adds["calendar_id"] == "⟨withheld⟩"
    assert adds["origin"] == "⟨withheld⟩"
    assert adds["destination"] == "⟨withheld⟩"
    assert parsed["decisions"][0]["raw_location"] == "⟨withheld⟩"
    assert parsed["checked"][0]["origin_latlng"] == "⟨withheld⟩"

    # A map keyed by calendar id keeps its shape and loses only the identity.
    capability = parsed["travel_blocks"]["apply"]["write_capability"]
    assert list(capability.values()) == ["writable"]
    assert list(capability) == ["⟨withheld #1⟩"]

    # The operator is told which fields, rather than left to spot the marker.
    note = page.locator(".audit-redaction-note")
    expect(note).to_have_count(1)
    expect(note).to_contain_text("Withheld from this dump")
    for field in ("calendar_id", "origin", "destination", "raw_location",
                  "origin_latlng", "write_capability (keys)"):
        expect(note).to_contain_text(field)


def _redaction_note_conforms_in_both_themes(page: Page) -> None:
    """--muted like every other secondary line, and no overflow at 390px."""
    for color_scheme in ("light", "dark"):
        page.emulate_media(color_scheme=color_scheme)
        expect(page.locator(".audit-redaction-note")).to_be_visible()
        colour = page.evaluate(
            "() => getComputedStyle(document.querySelector('.audit-redaction-note')).color"
        )
        muted_probe = page.evaluate(
            """
            () => {
              const m = getComputedStyle(document.documentElement)
                .getPropertyValue('--muted').trim();
              const el = document.createElement('span');
              el.style.color = m;
              document.body.appendChild(el);
              const c = getComputedStyle(el).color;
              el.remove();
              return c;
            }
            """
        )
        assert colour == muted_probe, f"{color_scheme}: note colour {colour} is not --muted"
        overflowing = page.evaluate(
            """
            () => {
              const vw = window.innerWidth;
              const bad = [];
              document.querySelectorAll('#runSheetCard *').forEach((el) => {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.right > vw + 0.5) bad.push(String(el.className));
              });
              return bad;
            }
            """
        )
        assert overflowing == [], f"{color_scheme}: redacted dump overflows 390px: {overflowing}"


def _redaction_phase(page: Page, base_url: str) -> None:
    _open_calendar_run(page, base_url, _run())
    parsed = _sentinels_stay_out_of_the_dom_but_the_dump_stays_diagnostic(page)
    _withheld_fields_are_marked(page, parsed)
    _redaction_note_conforms_in_both_themes(page)
    surface(page, "activity.run-sheet-redacted")


def _unknown_field_phase(page: Page, base_url: str) -> None:
    """A payload field nobody has thought about yet is withheld by default (a whitelist)."""
    run = _run()
    run["summary"]["some_field_invented_later"] = "SENTINELFUTUREFIELD"
    _open_calendar_run(page, base_url, run)

    assert "SENTINELFUTUREFIELD" not in page.content()
    expect(page.locator(".audit-redaction-note")).to_contain_text("some_field_invented_later")


@pytest.mark.smoke
@pytest.mark.live_safe
def test_calendar_run_sheet_reads_the_sweep_and_withholds_sensitive_fields(
    page: Page, base_url: str
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    _live_sweep_phase(page, base_url)
    reset_page(page)
    _gated_sweep_phase(page, base_url)
    reset_page(page)
    _redaction_phase(page, base_url)
    reset_page(page)
    _unknown_field_phase(page, base_url)
