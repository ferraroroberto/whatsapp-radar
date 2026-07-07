"""Browser smoke: the PWA shell loads, tabs switch, build identity renders."""

from __future__ import annotations

import re

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
def test_dashboard_metrics_render(page: Page, base_url: str) -> None:
    page.goto(base_url)
    # The Dashboard is the default pane; its metric cards are present…
    expect(page.locator("#paneDashboard")).to_contain_text("Channels monitored")
    # …and main.js fetches /api/dashboard, replacing the "–" placeholder with a
    # real count (0 on an empty DB), proving the metric round-trip works.
    expect(page.locator("#mChannels")).to_have_text(re.compile(r"^\d+$"))


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

    # The classifier config renders the read-only system prompt.
    page.locator("#configCard summary").click()
    expect(page.locator("#cfgPrompt")).to_contain_text("triage WhatsApp chat messages")


@pytest.mark.smoke
def test_build_readout_populates(page: Page, base_url: str) -> None:
    page.goto(base_url)
    # main.js fetches /api/version on boot and writes it into #buildReadout.
    page.locator("#settingsPanel").click()  # open the <details>
    expect(page.locator("#buildReadout")).to_contain_text("Build:")
