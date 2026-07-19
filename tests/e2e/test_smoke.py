"""Browser smoke: the PWA shell loads, tabs switch, build identity renders."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.smoke
def test_shell_loads(page: Page, base_url: str) -> None:
    page.goto(base_url)
    expect(page).to_have_title("WhatsApp Radar")
    expect(page.locator("#tabDashboard")).to_be_visible()
    # Dashboard is the default pane.
    expect(page.locator("#paneDashboard")).to_be_visible()
    expect(page.locator("#paneAudit")).to_be_hidden()


@pytest.mark.smoke
def test_tab_switching(page: Page, base_url: str) -> None:
    page.goto(base_url)
    page.locator("#tabAudit").click()
    expect(page.locator("#paneAudit")).to_be_visible()
    expect(page.locator("#paneDashboard")).to_be_hidden()
    page.locator("#tabChats").click()
    expect(page.locator("#paneChats")).to_be_visible()
    expect(page.locator("#paneAudit")).to_be_hidden()


@pytest.mark.smoke
def test_dashboard_activity_grid_renders(page: Page, base_url: str) -> None:
    page.goto(base_url)
    # The Dashboard is the default pane; the last-activity grid (#165) always
    # renders one card per kind from the unified run store, proving the
    # /api/dashboard round-trip works (cards read "never ran" on an unseeded DB).
    grid = page.locator("#dashActivity")
    expect(grid.locator(".activity-card")).to_have_count(4)
    for kind in ("WhatsApp", "Gmail", "Traffic", "Calendar"):
        expect(grid).to_contain_text(kind)
    # Monitored channels folded away by default so the grid is the hero (#165).
    expect(page.locator("#dashChannelsCard")).not_to_have_attribute("open", "")
    expect(page.locator("#dashChannelsBody")).to_be_hidden()


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
    row = page.locator(".chat-row", has_text="School Parents Group")
    expect(row).to_be_visible()
    toggle = row.locator(".chat-watch")
    before = toggle.get_attribute("aria-pressed")
    toggle.click()
    expected = "false" if before == "true" else "true"
    expect(toggle).to_have_attribute("aria-pressed", expected)

    # History overlay (read-only) for the seeded chat that has messages.
    page.locator(".chat-main", has_text="Class 4A Group").first.click()
    expect(page.locator("#historyOverlay")).to_be_visible()
    expect(page.locator("#historyBody")).to_contain_text("sample message")
    page.locator("#historyClose").click()
    expect(page.locator("#historyOverlay")).to_be_hidden()

    # Source selector isolates Gmail and the email overlay exposes subject/body/
    # thread evidence with an unmistakable source badge.
    page.locator("#chatsSourceGmail").click()
    gmail_row = page.locator(".chat-row", has_text="School Updates")
    expect(gmail_row).to_be_visible()
    expect(gmail_row.locator(".source-badge")).to_have_text("Gmail")
    gmail_row.locator(".chat-main").click()
    expect(page.locator("#historySource")).to_have_text("Gmail")
    expect(page.locator("#historyBody")).to_contain_text("Activity schedule")
    expect(page.locator("#historyBody")).to_contain_text("e2e-thread-1")
    page.locator("#historyClose").click()

    # The classifier config renders the read-only system prompt.
    page.locator("#configCard summary").click()
    expect(page.locator("#cfgPrompt")).to_contain_text(
        "triage new messages from a named communication channel"
    )
    expect(page.locator("#cfgGmailRoots")).not_to_be_empty()
    expect(page.locator("#cfgGmailTaxonomy")).not_to_be_empty()


@pytest.mark.smoke
def test_build_readout_populates(page: Page, base_url: str) -> None:
    page.goto(base_url)
    # main.js fetches /api/version on boot and writes it into the page footer,
    # which is visible on every tab (no settings panel to open).
    expect(page.locator("#buildReadout")).to_contain_text("Build:")
