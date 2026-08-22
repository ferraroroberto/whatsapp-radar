"""Family tab: the travel-block card's three-state write capability (#268).

The whole point of the card is that a household member's calendar state is
readable at a glance instead of out of a log — so `unknown` must render as its
own thing: not a green tick, not a red cross, not an omitted row. This drives
that in a real browser, in light and dark, against a route-mocked /api/family
payload (no DB seeding, no sweep, nothing mutating — hence `live_safe`).

Privacy: the mocked payload uses `example.com` placeholder calendars and
generic labels, never real household data, and the assertions are structural
(classes, glyph ids, computed colours) rather than content.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

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
    },
    "runs": [],
}


def _open_family(page: Page, base_url: str) -> None:
    page.route("**/api/family", lambda route: route.fulfill(json=_PAYLOAD))
    page.goto(base_url)
    page.wait_for_selector("#tabFamily", state="attached")
    page.locator("#tabFamily").click()
    page.wait_for_selector("#paneFamily", state="visible")


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

    overflowing = page.evaluate(
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
