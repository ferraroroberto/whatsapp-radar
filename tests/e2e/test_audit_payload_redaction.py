"""Browser e2e: the Audit raw payload dump withholds calendar ids and addresses (#285).

The dump used to end the family drill-down with `traceField('Run payload',
run.summary)` — the entire payload serialised verbatim. Since #263 that payload
carries a `travel_blocks` section whose entries each hold a `calendar_id`, and
in a real household those are email addresses; a travel leg's `origin` /
`destination` and an event's `raw_location` are literal street addresses. So
every purpose-built surface on the page honoured the no-calendar-id rule and the
generic dump immediately beneath them did not.

**Asserted on planted sentinels, not on the shape of an email address.** A
regex for `@` only rules out email-shaped values and would sail straight past a
street address — which is exactly the class of value most at risk here. Every
sensitive field carries a unique, unmistakable token instead, and the test
asserts none of them reaches the DOM by any route.

Route-mocked (hence ``live_safe``): no DB seeding, no run fired, no Google call.
The payload is invented — no household data appears in this file.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.smoke, pytest.mark.live_safe]

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


def _open(page: Page, base_url: str) -> None:
    run = _run()
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


def test_no_sentinel_reaches_the_dom_by_any_route(page: Page, base_url: str) -> None:
    """The criterion, stated as bluntly as it can be: none of it is on the page.

    Checked against the whole document's rendered text *and* its serialised
    HTML — an attribute or a hidden node would leak just as effectively as
    visible text, and a screenshot is not the only way a DOM travels.
    """
    _open(page, base_url)

    body_text = page.locator("body").inner_text()
    body_html = page.content()
    for sentinel in SENTINELS:
        assert sentinel not in body_text, f"{sentinel} rendered as text"
        assert sentinel not in body_html, f"{sentinel} present in the DOM"


def test_the_dump_still_carries_enough_to_diagnose_a_run(page: Page, base_url: str) -> None:
    """Redact, do not remove — the structured blocks are not a substitute.

    If this test can be satisfied by an empty dump, the redaction has gutted the
    thing it was supposed to protect.
    """
    _open(page, base_url)

    dump = page.locator(".audit-field", has_text="Run payload").locator("pre")
    text = dump.inner_text()
    parsed = json.loads(text)

    # The shape survives whole: every section, every count, every reason.
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


def test_a_withheld_field_is_marked_withheld_not_silently_dropped(
    page: Page, base_url: str
) -> None:
    """Silently dropping a field would let a redacted dump read as a complete one."""
    _open(page, base_url)

    dump = page.locator(".audit-field", has_text="Run payload").locator("pre")
    parsed = json.loads(dump.inner_text())

    # The key is still there; only its value is replaced.
    assert "calendar_id" in parsed["travel_blocks"]["adds"][0]
    assert parsed["travel_blocks"]["adds"][0]["calendar_id"] == "⟨withheld⟩"
    assert parsed["travel_blocks"]["adds"][0]["origin"] == "⟨withheld⟩"
    assert parsed["travel_blocks"]["adds"][0]["destination"] == "⟨withheld⟩"
    assert parsed["decisions"][0]["raw_location"] == "⟨withheld⟩"
    assert parsed["checked"][0]["origin_latlng"] == "⟨withheld⟩"

    # A map *keyed* by calendar id keeps its shape and loses only the identity:
    # how many calendars, and what state each is in, both survive.
    capability = parsed["travel_blocks"]["apply"]["write_capability"]
    assert list(capability.values()) == ["writable"]
    assert list(capability) == ["⟨withheld #1⟩"]

    # And the operator is told which fields, rather than left to spot the marker.
    note = page.locator(".audit-redaction-note")
    expect(note).to_have_count(1)
    expect(note).to_contain_text("Withheld from this dump")
    for field in ("calendar_id", "origin", "destination", "raw_location",
                  "origin_latlng", "write_capability (keys)"):
        expect(note).to_contain_text(field)


def test_an_unknown_future_field_is_withheld_by_default(page: Page, base_url: str) -> None:
    """The whole reason this is a whitelist (#285).

    A payload field nobody has thought about yet must be redacted by default,
    not exposed by default — otherwise this fix protects only the fields that
    existed on the day it was written.
    """
    run = _run()
    run["summary"]["some_field_invented_later"] = "SENTINELFUTUREFIELD"
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

    assert "SENTINELFUTUREFIELD" not in page.content()
    expect(page.locator(".audit-redaction-note")).to_contain_text("some_field_invented_later")


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_the_redaction_note_conforms_at_phone_width(
    page: Page, base_url: str, color_scheme: str
) -> None:
    """Existing tokens only, both themes, no horizontal overflow at 390px."""
    page.emulate_media(color_scheme=color_scheme)
    page.set_viewport_size({"width": 390, "height": 844})
    _open(page, base_url)

    note = page.locator(".audit-redaction-note")
    expect(note).to_be_visible()
    # --muted, like every other secondary line on the tab — not a warning colour.
    colour = page.evaluate(
        "() => getComputedStyle(document.querySelector('.audit-redaction-note')).color"
    )
    muted = page.evaluate(
        "() => getComputedStyle(document.documentElement).getPropertyValue('--muted').trim()"
    )
    probe = page.evaluate(
        """
        (m) => {
          const el = document.createElement('span');
          el.style.color = m;
          document.body.appendChild(el);
          const c = getComputedStyle(el).color;
          el.remove();
          return c;
        }
        """,
        muted,
    )
    assert colour == probe, f"note colour {colour} is not --muted ({probe})"

    overflowing = page.evaluate(
        """
        () => {
          const vw = window.innerWidth;
          const bad = [];
          document.querySelectorAll('#auditDetailCard *').forEach((el) => {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.right > vw + 0.5) bad.push(String(el.className));
          });
          return bad;
        }
        """
    )
    assert overflowing == [], f"redacted dump overflows 390px: {overflowing}"
