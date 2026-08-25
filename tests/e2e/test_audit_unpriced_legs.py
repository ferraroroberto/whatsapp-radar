"""Browser e2e: an unpriced commute leg never reads as a checked route (#283).

Two surfaces, one defect. The Audit run list's summary line and the family
drill-down's funnel cells both counted every entry in a traffic-check payload's
``checked`` list, including the legs that established nothing about the road —
``anchor_in_the_past`` (the drive is over, no Routes call was spent) and
``error`` (the call failed). A run in which *nothing* was priced therefore
reported a healthy-looking coverage number.

Route-mocked throughout (hence ``live_safe``): no DB seeding, no run fired, no
Routes call. The statuses under test are produced without a Routes call by
construction, so nothing here needs — or wants — a live backend. The payload
uses generic fixture names only.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.smoke, pytest.mark.live_safe]


def _leg(status: str) -> dict[str, Any]:
    return {
        "person": "parent-a",
        "event": "Class 4A Group meeting",
        "status": status,
        "anchor": "event_start",
        "eta_min": 20,
        "delay_min": 0,
    }


def _traffic_run(*statuses: str, alerts: int = 0) -> dict[str, Any]:
    return {
        "id": 9,
        "kind": "traffic-check",
        "summary": {
            "kind": "traffic-check",
            "status": "ok",
            "alerts": alerts,
            "checked": [_leg(status) for status in statuses],
        },
        "mode": "live",
        "status": "completed",
        "params": None,
        "started_at": "2026-08-20T07:00:00+00:00",
        "completed_at": "2026-08-20T07:00:12+00:00",
        "notification_status": "none",
        "error": None,
        "sources": {},
        "funnel": {},
    }


def _open_traffic_run(page: Page, base_url: str, run: dict[str, Any]) -> None:
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


def test_an_all_unpriced_run_never_shows_a_healthy_checked_count(
    page: Page, base_url: str
) -> None:
    """The headline case: three legs, none of them priced.

    Before #283 this list line read "3 checked · 0 alert(s)" — a coverage number
    that counted non-coverage, next to a zero alert count that looked reassuring
    precisely because nothing had been assessed.
    """
    _open_traffic_run(page, base_url, _traffic_run("error", "anchor_in_the_past", "error"))

    row = page.locator("#auditRuns .audit-run-li").first
    expect(row).to_contain_text("0 priced")
    expect(row).to_contain_text("3 not priced")
    expect(row).not_to_contain_text("3 checked")


def test_a_mixed_run_names_the_unpriced_half_in_the_list_and_the_funnel(
    page: Page, base_url: str
) -> None:
    """Some judged, some not — the granularity #270 had to fix on the Dashboard.

    Reporting only the judged half is the same all-clear-over-a-non-fact the
    unpriced statuses exist to prevent, just at run granularity.
    """
    _open_traffic_run(page, base_url, _traffic_run("ok", "ok", "error", alerts=1))

    row = page.locator("#auditRuns .audit-run-li").first
    expect(row).to_contain_text("2 priced")
    expect(row).to_contain_text("1 not priced")

    row.click()
    expect(page.locator("#auditDetailCard")).to_be_visible()
    # Labels are upper-cased by CSS, so the DOM text is the raw label.
    funnel = page.locator("#auditFunnel")
    expect(funnel.locator(".exec-funnel-cell", has_text="Not priced")).to_contain_text("1")
    expect(funnel).to_contain_text("Priced")
    expect(funnel).not_to_contain_text("Checked")


def test_a_fully_priced_run_gains_no_noise(page: Page, base_url: str) -> None:
    """The happy path must not become louder to make the unhappy one honest."""
    _open_traffic_run(page, base_url, _traffic_run("ok", "ok"))

    row = page.locator("#auditRuns .audit-run-li").first
    expect(row).to_contain_text("2 priced")
    expect(row).not_to_contain_text("not priced")


def test_a_legacy_payload_without_statuses_still_reads_as_priced(
    page: Page, base_url: str
) -> None:
    """Rows written before #270 carry no `status`, and were genuinely all priced.

    Defaulting them to "not priced" would invent a coverage gap that never
    existed — a different way of being confidently wrong about the same number.
    """
    run = _traffic_run("ok", "ok")
    for leg in run["summary"]["checked"]:
        del leg["status"]
    _open_traffic_run(page, base_url, run)

    row = page.locator("#auditRuns .audit-run-li").first
    expect(row).to_contain_text("2 priced")
    expect(row).not_to_contain_text("not priced")


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_the_split_funnel_fits_a_phone_in_both_themes(
    page: Page, base_url: str, color_scheme: str
) -> None:
    """A fourth funnel cell must not push the row off a 390px viewport."""
    page.emulate_media(color_scheme=color_scheme)
    page.set_viewport_size({"width": 390, "height": 844})
    _open_traffic_run(page, base_url, _traffic_run("ok", "error", "anchor_in_the_past"))

    page.locator("#auditRuns .audit-run-li").first.click()
    expect(page.locator("#auditDetailCard")).to_be_visible()
    expect(page.locator("#auditFunnel")).to_contain_text("Not priced")

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
    assert overflowing == [], f"split funnel overflows 390px: {overflowing}"
