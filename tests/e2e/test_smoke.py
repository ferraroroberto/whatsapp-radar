"""Browser smoke: the PWA shell loads, tabs switch, build identity renders."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.smoke
def test_shell_loads(page: Page, base_url: str) -> None:
    page.goto(base_url)
    expect(page).to_have_title("📡 WhatsApp Radar")
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
def test_build_readout_populates(page: Page, base_url: str) -> None:
    page.goto(base_url)
    # main.js fetches /api/version on boot and writes it into #buildReadout.
    page.locator("#settingsPanel").click()  # open the <details>
    expect(page.locator("#buildReadout")).to_contain_text("Build:")
