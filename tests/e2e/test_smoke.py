"""Browser smoke: the PWA shell loads, tabs switch, build identity renders."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.smoke
@pytest.mark.live_safe
def test_shell_loads(page: Page, base_url: str) -> None:
    page.goto(base_url)
    expect(page).to_have_title("Family Radar")
    expect(page.locator("#tabDashboard")).to_be_visible()
    # Dashboard is the default pane.
    expect(page.locator("#paneDashboard")).to_be_visible()
    expect(page.locator("#paneActivity")).to_be_hidden()


@pytest.mark.smoke
@pytest.mark.live_safe
def test_tab_switching(page: Page, base_url: str) -> None:
    # Four tabs, Activity second (#373: Run and Audit merged); the fleet nav
    # allows at most five.
    page.goto(base_url)
    tabs = page.locator("nav.tabs [role=tab]")
    expect(tabs).to_have_count(4)
    expect(tabs.nth(1)).to_have_attribute("data-tab", "activity")
    expect(page.locator("#tabExecution, #tabAudit, #tabAck")).to_have_count(0)
    # Follow-ups are Telegram-only since #374: no in-app card either.
    expect(page.locator("#ackItems")).to_have_count(0)
    # Every tab's header line names a fact once its data lands (#374).
    has_words = re.compile(r"\w")
    expect(page.locator("#dashStatus")).to_have_text(has_words)
    page.locator("#tabActivity").click()
    expect(page.locator("#paneActivity")).to_be_visible()
    expect(page.locator("#paneDashboard")).to_be_hidden()
    expect(page.locator("#activityStatus")).to_have_text(has_words)
    # Activity opens on Messages.
    expect(page.locator('#activityScope [data-scope="messages"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    page.locator("#tabChats").click()
    expect(page.locator("#paneChats")).to_be_visible()
    expect(page.locator("#paneActivity")).to_be_hidden()
    expect(page.locator("#chatsStatus")).to_contain_text("monitored")
    page.locator("#tabFamily").click()
    expect(page.locator("#familyStatus")).to_have_text(has_words)


@pytest.mark.smoke
@pytest.mark.live_safe
def test_a_stored_run_or_audit_tab_opens_activity(page: Page, base_url: str) -> None:
    # An installed PWA that last had Run or Audit open reopens on Activity.
    page.goto(base_url)
    for old in ("execution", "audit"):
        page.evaluate("k => localStorage.setItem('wa-radar.tab', k)", old)
        page.reload()
        expect(page.locator("#paneActivity")).to_be_visible()


def _card(source: str, kind: str | None, run_id: int | None, status: str | None,
          alerts: int = 0, summary: str = "") -> dict[str, object]:
    return {"source": source, "kind": kind, "db_run_id": run_id,
            "started_at": "2026-06-20T18:00:00+00:00" if kind else None,
            "status": status, "alerts": alerts, "summary": summary}


@pytest.mark.smoke
@pytest.mark.live_safe
def test_home_glance_card_links_into_activity(page: Page, base_url: str) -> None:
    """Home is one glance card plus Latest runs (#374): a row per source with
    exception chips only, a header line naming the worst exception, and every
    row and that line opening the run's sheet in Activity at its scope."""
    dashboard = {
        "last_activity": [
            _card("whatsapp", "scan", 4, "completed", 0, "12 new · 0 actionable"),
            _card("gmail", "scan", 7, "failed", 0, "0 new · 0 actionable"),
            _card("traffic", "traffic-check", 9, "completed", 1, "1 delay alert"),
            _card("calendar", None, None, None),
        ],
        "scans": {"last": None},
        "sources": [],
    }
    health = {"sources": [
        {"source": "whatsapp", "enabled": True, "connected": True},
        {"source": "gmail", "enabled": True, "connected": True},
        {"source": "calendar", "enabled": False, "connected": False},
    ]}
    run7 = {"id": 7, "kind": "scan", "summary": None, "mode": "live", "status": "failed",
            "params": None, "started_at": "2026-06-20T18:00:00+00:00",
            "completed_at": "2026-06-20T18:01:00+00:00", "notification_status": "none",
            "error": "connector offline", "sources": {}, "funnel": {}}
    page.route("**/api/dashboard", lambda route: route.fulfill(json=dashboard))
    page.route("**/api/execution/health", lambda route: route.fulfill(json=health))
    page.route("**/api/sidecar/status", lambda route: route.fulfill(
        json={"is_live": True, "state": "connected", "has_qr": False}))
    page.route("**/api/audit/runs/7", lambda route: route.fulfill(json={"run": run7, "traces": []}))
    page.goto(base_url)

    pane = page.locator("#paneDashboard")
    expect(pane.locator(".home-head .home-title")).to_have_text("Home")
    rows = page.locator("#dashGlance .glance-row")
    expect(rows).to_have_count(4)
    for i, name in enumerate(("WhatsApp", "Gmail", "Calendar", "Traffic")):
        expect(rows.nth(i).locator(".action-row-title")).to_have_text(name)
    # Exceptions only: no OK anywhere, a Failed chip, the traffic alert chip,
    # and the healthy WhatsApp row carries no chip and a live badge.
    expect(pane).not_to_contain_text("OK")
    expect(rows.nth(1).locator(".status-chip")).to_have_text("Failed")
    expect(rows.nth(1).locator(".status-chip")).to_have_attribute("data-tone", "danger")
    expect(rows.nth(3).locator(".status-chip")).to_have_attribute("data-tone", "attention")
    expect(rows.nth(0).locator(".status-chip")).to_have_count(0)
    expect(rows.nth(0).locator(".action-row-avatar")).to_have_attribute("data-badge", "up")
    # The Monitored table left Home; the source counts moved into Settings.
    expect(page.locator("#dashChannelsCard")).to_have_count(0)
    expect(page.locator("#settingsDialog #dashSourcesCard")).to_have_count(1)
    expect(page.locator("#dashAllActivity")).to_be_visible()

    # The header names the worst exception and opens that run in Activity.
    status = page.locator("#dashStatus")
    expect(status).to_have_text("Gmail scan failed")
    expect(status).to_have_attribute("data-tone", "danger")
    status.click()
    expect(page.locator("#paneActivity")).to_be_visible()
    expect(page.locator('#activityScope [data-scope="messages"]')).to_have_attribute(
        "aria-pressed", "true")
    expect(page.locator("#runSheetCard")).to_be_visible()
    expect(page.locator("#runSheetTitle")).to_contain_text("#7")

    # A glance row lands on its own scope.
    page.locator("#runSheetClose").click()
    page.locator("#tabDashboard").click()
    rows.nth(3).locator(".action-row-main").click()
    expect(page.locator('#activityScope [data-scope="traffic"]')).to_have_attribute(
        "aria-pressed", "true")
    expect(page.locator("#runSheetCard")).to_be_visible()


# Not @pytest.mark.live_safe (issue #225): clicks the unmocked .chat-watch
# toggle, which would flip a real chat's monitored status via a real,
# un-mocked API call if pointed at the live tray.
@pytest.mark.smoke
def test_chats_tab_toggle_history_and_prompt(page: Page, base_url: str) -> None:
    page.goto(base_url)
    page.locator("#tabChats").click()
    expect(page.locator("#paneChats")).to_be_visible()

    # The session DB is shared + persistent across the two browser runs, so this
    # test must be order-independent: use the "All" filter (every chat shows
    # regardless of status) and assert the toggle label simply *flips*, whatever
    # the starting state left by a prior run.
    page.locator("#chatsFilterAll").click()
    # The status scope is the segmented control: the raised segment is the state.
    expect(page.locator("#chatsFilterAll")).to_have_attribute("aria-pressed", "true")
    expect(page.locator("#chatsFilterMonitored")).to_have_attribute("aria-pressed", "false")
    row = page.locator(".chat-row", has_text="School Parents Group")
    expect(row).to_be_visible()
    toggle = row.locator(".chat-watch")
    before = toggle.get_attribute("aria-pressed")
    toggle.click()
    expected = "false" if before == "true" else "true"
    expect(toggle).to_have_attribute("aria-pressed", expected)

    # The row is one fleet row (#375): avatar, name, one line of time + preview,
    # a plain watch icon button; no outlined source pill and no message count.
    expect(row.locator(".chat-avatar")).to_be_visible()
    expect(row.locator(".source-badge")).to_have_count(0)
    expect(row).not_to_contain_text("msgs")
    # Inbox opens on one scope switch and one filter field; no card heading.
    expect(page.locator("#paneChats .card-title")).to_have_count(0)
    expect(page.locator("#chatsSearch")).to_be_visible()

    # The chat sheet (read-only) for the seeded chat that has messages.
    page.locator(".chat-main", has_text="Class 4A Group").first.click()
    expect(page.locator("#historyCard")).to_be_visible()
    expect(page.locator("#historySubtitle")).to_contain_text("messages")
    expect(page.locator("#historyBody")).to_contain_text("sample message")
    # TOUCH-02 (#375): the sheet header's actions never overlap at phone width —
    # each icon button's 44px target, centred on its box, clears its neighbour.
    page.set_viewport_size({"width": 390, "height": 844})
    targets = page.locator(".history-card .detail-header button").evaluate_all(
        """els => els.filter((e) => e.offsetParent !== null).map((e) => {
            const r = e.getBoundingClientRect();
            const cx = r.left + r.width / 2;
            return [Math.min(r.left, cx - 22), Math.max(r.right, cx + 22)];
        })"""
    )
    assert len(targets) >= 2, targets
    for (_, right), (left, _) in zip(targets, targets[1:], strict=False):
        assert right <= left + 0.5, f"overlapping header targets: {targets}"
    # One labelled menu: Rename…, Link chats… and the monitor toggle.
    page.locator("#historyMenu").click()
    menu = page.get_by_role("menu")
    expect(menu.get_by_role("menuitem", name="Rename…")).to_be_visible()
    expect(menu.get_by_role("menuitem", name="Link chats…")).to_be_visible()
    expect(menu.get_by_role("menuitem", name="Monitor")).to_be_visible()
    page.keyboard.press("Escape")
    expect(menu).to_have_count(0)
    page.locator("#historyClose").click()
    expect(page.locator("#historyCard")).to_be_hidden()

    # Source selector isolates Gmail and the email overlay exposes subject/body/
    # thread evidence with an unmistakable source badge.
    page.locator("#chatsSource").select_option("gmail")
    # The count line shows only while the filter narrows the list.
    expect(page.locator("#chatsCount")).to_contain_text("match")
    gmail_row = page.locator(".chat-row", has_text="School Updates")
    expect(gmail_row).to_be_visible()
    gmail_row.locator(".chat-main").click()
    expect(page.locator("#historySubtitle")).to_contain_text("Gmail")
    expect(page.locator("#historyBody")).to_contain_text("Activity schedule")
    expect(page.locator("#historyBody")).to_contain_text("e2e-thread-1")
    page.locator("#historyClose").click()

    # The classifier config renders the read-only system prompt.
    page.locator("#paneChats .home-settings").click()
    expect(page.locator("#settingsDialog")).to_be_visible()
    page.locator("#configCard summary").click()
    expect(page.locator("#cfgPrompt")).to_contain_text(
        "triage new messages from a named communication channel"
    )
    expect(page.locator("#cfgGmailRoots")).not_to_be_empty()
    expect(page.locator("#cfgGmailTaxonomy")).not_to_be_empty()


@pytest.mark.smoke
@pytest.mark.live_safe
def test_build_readout_populates(page: Page, base_url: str) -> None:
    page.goto(base_url)
    # main.js fetches /api/version on boot and writes it into the page footer,
    # which is visible on every tab (no settings panel to open).
    expect(page.locator("#buildReadout")).to_contain_text("Build:")
