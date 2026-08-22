"""Family tab: the travel-block card's write capability (#268) and run controls (#276).

The whole point of the card is that a household member's calendar state is
readable at a glance instead of out of a log — so `unknown` must render as its
own thing: not a green tick, not a red cross, not an omitted row. #276 adds the
two run controls and the reasons a live sweep is unavailable. This drives both
in a real browser, in light and dark, against a route-mocked /api/family payload
(no DB seeding, no sweep, nothing mutating — hence `live_safe`).

Every `/api/execution/**` request is aborted by a catch-all route registered
first, with only the two endpoints under test fulfilled on top of it. No test
here may reach a real backend: firing a real `calendar-scan` would spend Routes
quota and, out of dry run, write to real calendars.

Privacy: the mocked payload uses `example.com` placeholder calendars and
generic labels, never real household data, and the assertions are structural
(classes, glyph ids, computed colours) rather than content.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

pytestmark = [pytest.mark.smoke, pytest.mark.live_safe]

_PAYLOAD = {
    "traffic": {
        "enabled": False, "api_key_set": True, "significant_delay_min": 15,
        "cadence_min": 30, "quiet_start_hour": 22, "quiet_end_hour": 7,
        "dedup_window_min": 180, "effective_dedup_window_min": 180,
        "origin_lookback_min": 120, "lookahead_hours": 3,
        "skip_leave_now_for_train": True, "train_keywords": ["train"],
        "last_check": None, "last_alert": None,
    },
    "family": {
        "enabled": True, "run_hour": 7, "home_address": "", "kids_home_time": "17:30",
        "responsible_by_weekday": {}, "childcare_windows": [],
        "unknown_scan_days": 7, "assessment_days": 2, "ask_missing_locations": True,
    },
    "calendars": [
        {"person": "parent-a", "calendar_id": "parent-a@example.com", "label": "Parent A"},
        {"person": "parent-b", "calendar_id": "parent-b@example.com", "label": "Parent B"},
        {"person": "parent-c", "calendar_id": "parent-c@example.com", "label": "Parent C"},
    ],
    "token_present": True,
    "travel_blocks": {
        "enabled": True,
        "dry_run": True,
        "horizon_days": 2,
        "min_home_dwell_min": 45,
        "title_template": "Commute",
        "max_title_template": 60,
        "write_token_present": True,
        "write_capability": [
            {"person": "parent-a", "label": "Parent A",
             "calendar_id": "parent-a@example.com", "state": "writable"},
            {"person": "parent-b", "label": "Parent B",
             "calendar_id": "parent-b@example.com", "state": "not_writable"},
            {"person": "parent-c", "label": "Parent C",
             "calendar_id": "parent-c@example.com", "state": "unknown"},
        ],
        "last_sweep": {
            "run_id": "db-1", "started_at": "2026-08-20T06:00:00+00:00", "status": "ok",
            "dry_run": True, "routes_calls": 3,
            "counts": {"desired": 4, "adds": 2, "deletes": 1,
                       "keeps": 1, "protected": 0, "failures": 1},
            "apply": {
                "status": "dry_run",
                "counts": {"inserted": 0, "deleted": 0, "kept": 1,
                           "skipped": 3, "backups": 0},
                "failures": 0,
            },
        },
        # Dry run is on in this payload, so the live control is unavailable and
        # says so (#276). The server refuses for the same reason regardless.
        "live_sweep_blockers": [
            {"code": "dry_run",
             "message": "Dry run is on, so a sweep plans only. "
                        "Turn it off and save before running a live sweep."},
        ],
    },
    "runs": [],
}


def _payload(**travel_blocks: Any) -> dict[str, Any]:
    """A copy of the sanitized payload with travel-block keys overridden."""
    out = copy.deepcopy(_PAYLOAD)
    out["travel_blocks"].update(copy.deepcopy(travel_blocks))
    return out


def _open_family(page: Page, base_url: str, payload: dict[str, Any] | None = None) -> None:
    # Nothing under /api/execution may reach the server from this file. The
    # catch-all goes on first so a later, more specific route wins (Playwright
    # matches most-recently-registered first) and anything unforeseen aborts
    # rather than firing a real calendar-scan.
    page.route("**/api/execution/**", lambda route: route.abort())
    page.route("**/api/family", lambda route: route.fulfill(json=payload or _PAYLOAD))
    page.goto(base_url)
    page.wait_for_selector("#tabFamily", state="attached")
    page.locator("#tabFamily").click()
    page.wait_for_selector("#paneFamily", state="visible")


def _no_overflow(page: Page) -> list[str]:
    return page.evaluate(
        """
        () => {
          const vw = window.innerWidth;
          const bad = [];
          document.querySelectorAll('#familyTravelCard *').forEach((el) => {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.right > vw + 0.5) bad.push(String(el.className));
          });
          return bad;
        }
        """
    )


def _glyph(page: Page, selector: str) -> str:
    return page.evaluate(
        "(sel) => document.querySelector(sel).querySelector('use').getAttribute('href')",
        selector,
    )


def _color(page: Page, selector: str) -> str:
    return page.evaluate(
        "(sel) => getComputedStyle(document.querySelector(sel)).color", selector
    )


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_unknown_write_capability_is_its_own_visible_state(
    page: Page, base_url: str, color_scheme: str
) -> None:
    page.emulate_media(color_scheme=color_scheme)
    # Phone-first surface: the badge + its explanatory line have to fit the
    # narrowest supported width, where a pill next to long text is most likely
    # to push the pane sideways.
    page.set_viewport_size({"width": 390, "height": 844})
    _open_family(page, base_url)

    card = page.locator("#familyTravelCard")
    expect(card).to_be_visible()

    # All three calendars are listed — an unresolved probe is never dropped.
    rows = page.locator("#familyTravelBlocks .tb-cap-row")
    expect(rows).to_have_count(3)
    expect(rows.nth(2)).to_contain_text("Parent C")
    expect(rows.nth(2)).to_contain_text("Unknown")

    writable = "#familyTravelBlocks .tb-cap--writable"
    refused = "#familyTravelBlocks .tb-cap--not-writable"
    unknown = "#familyTravelBlocks .tb-cap--unknown"
    for selector in (writable, refused, unknown):
        expect(page.locator(selector)).to_have_count(1)

    # Distinct glyph: never the tick, never the cross.
    assert _glyph(page, unknown) not in {_glyph(page, writable), _glyph(page, refused)}
    assert _glyph(page, unknown) not in {"#i-check", "#i-x"}

    # Distinct colour in both themes...
    colors = {_color(page, writable), _color(page, refused), _color(page, unknown)}
    assert len(colors) == 3, f"the three states share a colour in {color_scheme}: {colors}"

    # ...and distinct with colour stripped out: only `unknown` is dashed.
    styles = page.evaluate(
        """
        (sels) => sels.map((s) => getComputedStyle(document.querySelector(s)).borderTopStyle)
        """,
        [writable, refused, unknown],
    )
    assert styles == ["solid", "solid", "dashed"], styles

    overflowing = _no_overflow(page)
    assert overflowing == [], f"travel-block card overflows 390px: {overflowing}"


def test_dry_run_mode_is_unmistakable(page: Page, base_url: str) -> None:
    """A sweep that plans but does not write must not read like one that writes."""
    _open_family(page, base_url)
    banner = page.locator("#familyTravelBlocks .tb-mode")
    expect(banner).to_have_class("tb-mode tb-mode--dry")
    expect(banner).to_contain_text("Dry run")
    expect(banner).to_contain_text("Nothing is written to any calendar.")
    # The last sweep says so too, independently of the current setting.
    expect(page.locator("#familyTravelBlocks")).to_contain_text("planned only, nothing written")


# --------------------------------------------------- run controls (#276)

# Distinct from the payload's own sweep (4 legs / 2 adds) so "the card
# re-read /api/family" is provable from the rendered numbers alone — no
# timestamp comparison, which would depend on the browser's time zone.
_AFTER_SWEEP = {
    "run_id": "db-9", "started_at": "2026-08-21T12:00:00+00:00", "status": "ok",
    "dry_run": True, "routes_calls": 2,
    "counts": {"desired": 7, "adds": 5, "deletes": 1,
               "keeps": 1, "protected": 1, "failures": 1},
    "apply": None,
}

_FINISHED_RUN = {
    "kind": "calendar-scan",
    "run_id": "fs-1",
    "status": "completed",
    "mode": "dry_run",
    "started_at": "2026-08-21T12:00:00+00:00",
    "finished_at": "2026-08-21T12:00:20+00:00",
    "error": None,
    "result": {
        "kind": "calendar-scan",
        "status": "ok",
        "travel_blocks": {
            "status": "ok",
            "dry_run": True,
            "routes_calls": 2,
            "counts": {"desired": 7, "adds": 5, "deletes": 1,
                       "keeps": 1, "protected": 1, "failures": 1},
        },
    },
}


def _drive_sweep(
    page: Page,
    base_url: str,
    *,
    posted: list[dict[str, Any]],
    run_status: int = 200,
    after: dict[str, Any] | None = None,
) -> None:
    """Open the Family tab with the two execution endpoints the card uses mocked.

    ``after`` is the ``last_sweep`` the *next* /api/family read returns once the
    run has been posted — keyed on the POST actually happening, not on a call
    count, so a boot-time re-read can't hand the test its own answer early.
    """
    fired = {"run": False}

    def on_family(route: Route) -> None:
        if fired["run"] and after is not None:
            route.fulfill(json=_payload(last_sweep=after))
            return
        route.fulfill(json=_PAYLOAD)

    def on_run(route: Route) -> None:
        fired["run"] = True
        posted.append(json.loads(route.request.post_data or "{}"))
        if run_status != 200:
            route.fulfill(status=run_status, json={"detail": "a run is already in progress"})
            return
        route.fulfill(json={"kind": "calendar-scan", "run_id": "fs-1"})

    page.route("**/api/execution/**", lambda route: route.abort())
    page.route("**/api/execution/runs**", lambda route: route.fulfill(
        json={"active": None, "runs": [_FINISHED_RUN], "skipped_count": 0}
    ))
    page.route("**/api/execution/run", on_run)
    page.route("**/api/family", on_family)
    page.goto(base_url)
    page.wait_for_selector("#tabFamily", state="attached")
    page.locator("#tabFamily").click()
    page.wait_for_selector("#paneFamily", state="visible")


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_live_sweep_control_is_unavailable_with_a_stated_reason(
    page: Page, base_url: str, color_scheme: str
) -> None:
    """Not a silent grey-out: the button is off *and* the card says why.

    The stated reason also says what the button is not — a courtesy, never the
    thing standing between a click and a calendar write.
    """
    page.emulate_media(color_scheme=color_scheme)
    page.set_viewport_size({"width": 390, "height": 844})
    _open_family(page, base_url)

    buttons = page.locator("#familyTravelBlocks .tb-actions .run-btn")
    expect(buttons).to_have_count(2)
    expect(buttons.nth(0)).to_contain_text("Rehearse")
    expect(buttons.nth(1)).to_contain_text("Run sweep")
    expect(buttons.nth(0)).to_be_enabled()
    expect(buttons.nth(1)).to_be_disabled()

    card = page.locator("#familyTravelCard")
    expect(card).to_contain_text("A live sweep is unavailable")
    expect(card).to_contain_text("this button is not what is stopping it")
    expect(card).to_contain_text("Dry run is on")

    overflowing = _no_overflow(page)
    assert overflowing == [], f"run controls overflow 390px: {overflowing}"


def test_live_sweep_control_is_available_once_every_gate_clears(
    page: Page, base_url: str
) -> None:
    _open_family(page, base_url, _payload(dry_run=False, live_sweep_blockers=[]))
    buttons = page.locator("#familyTravelBlocks .tb-actions .run-btn")
    expect(buttons.nth(1)).to_be_enabled()
    expect(page.locator("#familyTravelCard")).not_to_contain_text("A live sweep is unavailable")


def test_rehearse_fires_a_dry_run_calendar_scan_and_refreshes_the_card(
    page: Page, base_url: str
) -> None:
    """One POST of the existing verb — no travel-blocks-only endpoint (#266).

    And "Last sweep" moves without a page reload, which is the only way the
    operator can tell the run they just asked for is the one they are reading.
    """
    posted: list[dict[str, Any]] = []
    _drive_sweep(page, base_url, posted=posted, after=_AFTER_SWEEP)

    blocks = page.locator("#familyTravelBlocks")
    expect(blocks).to_contain_text("4 leg(s) · 2 add")
    blocks.locator(".tb-actions .run-btn").nth(0).click()

    status = blocks.locator(".tb-run-status")
    expect(status).to_contain_text("Sweep finished", timeout=15_000)
    expect(status).to_contain_text("planned only, nothing written")
    expect(status).to_contain_text("1 left alone")

    assert posted == [{"action": "calendar-scan", "mode": "dry_run"}]
    expect(blocks).to_contain_text("7 leg(s) · 5 add")


def test_a_concurrent_run_renders_as_already_running(page: Page, base_url: str) -> None:
    """409 is the single-flight guard working, not a failure."""
    posted: list[dict[str, Any]] = []
    _drive_sweep(page, base_url, posted=posted, run_status=409)

    page.locator("#familyTravelBlocks .tb-actions .run-btn").nth(0).click()
    status = page.locator("#familyTravelBlocks .tb-run-status")
    expect(status).to_contain_text("already in progress", timeout=15_000)
    expect(status).not_to_contain_text("Could not start")
    expect(status).not_to_have_class(re.compile("tb-run-status--error"))
    # The control comes back — a busy queue is temporary, not terminal.
    expect(page.locator("#familyTravelBlocks .tb-actions .run-btn").nth(0)).to_be_enabled()


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_gated_sweep_reads_as_gated_never_as_zeros(
    page: Page, base_url: str, color_scheme: str
) -> None:
    """`dry_run: null` / `counts: null` must not be painted as a clean sweep."""
    page.emulate_media(color_scheme=color_scheme)
    page.set_viewport_size({"width": 390, "height": 844})
    _open_family(page, base_url, _payload(last_sweep={
        "run_id": "db-4", "started_at": "2026-08-20T12:00:00+00:00",
        "status": "disabled", "dry_run": None, "routes_calls": 0,
        "counts": None, "apply": None,
    }))

    card = page.locator("#familyTravelBlocks")
    expect(card).to_contain_text("gated — travel blocks are off")
    expect(card).to_contain_text("No plan was computed")
    expect(card).not_to_contain_text("0 add")
    expect(card).not_to_contain_text("planned only, nothing written")

    overflowing = _no_overflow(page)
    assert overflowing == [], f"gated sweep overflows 390px: {overflowing}"
