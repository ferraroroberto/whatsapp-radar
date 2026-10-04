"""App shell: every pane opens with the vendored home-head (#337).

design.md `page-header`: tab title, trailing theme toggle and a Settings gear,
always both, on every tab; Settings is never a tab. The gear opens the Settings
dialog, which hosts the classifier config and maintenance cards.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.smoke, pytest.mark.live_safe]

_TABS = ["Dashboard", "Chats", "Execution", "Audit", "Family"]


def test_every_pane_has_theme_toggle_and_settings_gear(
    page: Page, base_url: str, scaled: Callable[[float], int]
) -> None:
    page.goto(base_url)
    page.wait_for_selector("#tabDashboard", state="attached", timeout=scaled(10_000))
    for name in _TABS:
        page.locator(f"#tab{name}").click()
        head = page.locator(f"#pane{name} > .home-head")
        expect(head).to_be_visible()
        expect(head.locator(".theme-toggle")).to_have_count(1)
        expect(head.locator(".home-settings")).to_have_count(1)
    # Settings is a gear, never a tab.
    expect(page.locator("nav.tabs [data-tab=settings]")).to_have_count(0)


def test_gear_opens_settings_with_config_and_maintenance(
    page: Page, base_url: str, scaled: Callable[[float], int]
) -> None:
    page.goto(base_url)
    page.wait_for_selector("#tabDashboard", state="attached", timeout=scaled(10_000))
    page.locator("#tabAudit").click()
    page.locator("#paneAudit .home-settings").click()
    dialog = page.locator("#settingsDialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#settingsTitle")).to_be_visible()
    expect(dialog.locator("#configCard")).to_be_visible()
    expect(dialog.locator("#execMaintenanceCard")).to_be_visible()
    page.locator("#settingsClose").click()
    expect(dialog).not_to_be_visible()
