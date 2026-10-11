"""App shell: every pane opens with the vendored home-head (#337).

design.md `page-header`: tab title, trailing theme toggle and a Settings gear,
always both, on every tab; Settings is never a tab. The gear opens the Settings
dialog, which hosts the classifier config and maintenance cards.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Page, expect

pytestmark = [pytest.mark.smoke, pytest.mark.live_safe]

_TABS = ["Dashboard", "Activity", "Chats", "Family"]
# .icon-button's (project-scaffolding#339): a glyph on nothing at rest.
_UNPAINTED = """els => els.map(el => {
    const s = getComputedStyle(el);
    return [s.backgroundColor, s.borderTopWidth];
})"""
_CLEAR = ["rgba(0, 0, 0, 0)", "0px"]


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
        # Activity adds its Run-now action (#373); every header action is unpainted.
        assert all(s == _CLEAR for s in head.locator(".home-toggle").evaluate_all(_UNPAINTED))
    # Settings is a gear, never a tab.
    expect(page.locator("nav.tabs [data-tab=settings]")).to_have_count(0)


def test_gear_opens_settings_with_config_and_maintenance(
    page: Page, base_url: str, scaled: Callable[[float], int]
) -> None:
    page.goto(base_url)
    page.wait_for_selector("#tabDashboard", state="attached", timeout=scaled(10_000))
    page.locator("#tabActivity").click()
    page.locator("#paneActivity .home-settings").click()
    dialog = page.locator("#settingsDialog")
    expect(dialog).to_be_visible()
    expect(dialog.locator("#settingsTitle")).to_be_visible()
    # The header line (#374): a source that should be up and is not, else the
    # build; the title never truncates beside it.
    status = dialog.locator("#settingsStatus")
    expect(status).to_have_text(re.compile(r"offline$|^Build: "))
    assert dialog.locator("#settingsTitle").evaluate(
        "el => el.scrollWidth <= el.clientWidth"
    ), "Settings title truncates beside its status line"
    # The per-source counts card moved here from Home (#374).
    expect(dialog.locator("#dashSourcesCard")).to_be_visible()
    expect(dialog.locator("#configCard")).to_be_visible()
    expect(dialog.locator("#execMaintenanceCard")).to_be_visible()
    # Recent syncs and Sources health moved here from the Run tab (#373).
    expect(dialog.locator("#execSyncsCard")).to_be_visible()
    expect(dialog.locator("#execSourcesCard")).to_be_visible()
    assert dialog.locator("#settingsClose").evaluate_all(_UNPAINTED) == [_CLEAR]
    page.locator("#settingsClose").click()
    expect(dialog).not_to_be_visible()
