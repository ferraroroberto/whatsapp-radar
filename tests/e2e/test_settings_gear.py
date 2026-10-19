"""App shell: every pane opens with the vendored home-head (#337).

design.md `page-header`: tab title, trailing theme toggle and a Settings gear,
always both, on every tab; Settings is never a tab. The gear opens the Settings
dialog: inset groups whose rows open sheets that save on Done (#377).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e._surface import surface

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


_GROUPS = ["Display", "Sources", "How messages are judged", "Alerts", "Maintenance",
           "This install"]


def test_gear_opens_settings_with_its_groups(
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
    # The six groups (#377), and no file path or stage number in any label.
    expect(dialog.locator(".overline")).to_have_text(_GROUPS)
    labels = dialog.locator(".action-row-title").all_inner_texts()
    assert labels and not [x for x in labels if "/" in x or re.search(r"stage", x, re.I)], labels
    assert dialog.locator("#settingsClose").evaluate_all(_UNPAINTED) == [_CLEAR]
    surface(page, "settings.dialog")
    # Counts (#374), health and recent syncs (#373) live in each source's sheet.
    dialog.locator('[data-settings-sheet="source-whatsapp"]').click()
    sheet = page.locator("#sourceSheet")
    expect(sheet).to_be_visible()
    for part in ("#dashSources", "#execSources", "#execSyncs"):
        expect(sheet.locator(part)).to_have_count(1)
    page.keyboard.press("Escape")
    expect(sheet).to_be_hidden()
    page.locator("#settingsClose").click()
    expect(dialog).not_to_be_visible()


# TOUCH-01 (#377), structural only: every input and select Settings can show â€”
# every disclosure opened, every sheet a row opens â€” is at least 44px tall.
_SHORT_CONTROLS = """(roots) => {
    const bad = [];
    for (const root of roots) {
        const scope = document.querySelector(root);
        if (!scope) continue;
        for (const el of scope.querySelectorAll('input:not([type=hidden]), select')) {
            if (!el.getClientRects().length) continue;
            const h = el.getBoundingClientRect().height;
            const name = el.id || el.tagName.toLowerCase();
            if (h < 43.5) bad.push(root + ' ' + name + ' ' + Math.round(h));
        }
    }
    return bad;
}"""


def test_settings_controls_meet_the_touch_floor(
    page: Page, base_url: str, scaled: Callable[[float], int]
) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(base_url)
    page.wait_for_selector("#tabDashboard", state="attached", timeout=scaled(10_000))
    page.locator("#paneDashboard .home-settings").click()
    expect(page.locator("#settingsDialog")).to_be_visible()
    page.wait_for_timeout(scaled(500))  # /api/config fills the rows
    page.evaluate(
        "document.querySelectorAll('#settingsDialog details').forEach(d => { d.open = true; })"
    )
    offenders = page.evaluate(_SHORT_CONTROLS, ["#settingsDialog"])
    openers = page.locator("#settingsDialog [data-settings-sheet]")
    for i in range(openers.count()):
        openers.nth(i).click()
        sheet = page.locator("dialog.edit-sheet[open]")
        expect(sheet).to_have_count(1)
        offenders += page.evaluate(_SHORT_CONTROLS, ["dialog.edit-sheet[open]"])
        page.keyboard.press("Escape")
        expect(sheet).to_have_count(0)
    assert offenders == [], f"Settings controls under 44px: {offenders}"


def _settings_with_posts(page: Page, base_url: str, posted: list[dict[str, Any]],
                         *, reject: str | None = None) -> None:
    """Settings over the autobooted app; POST /api/config is captured, never sent."""

    def on_config(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        posted.append(json.loads(route.request.post_data or "{}"))
        if reject:
            route.fulfill(status=400, json={"detail": reject})
        else:
            route.fulfill(json={"ok": True})

    page.route("**/api/config", on_config)
    page.goto(base_url)
    page.wait_for_selector("#tabDashboard", state="attached")
    page.locator("#paneDashboard .home-settings").click()
    expect(page.locator('#settingsDialog [data-value="hub"]')).not_to_be_empty()


def test_a_settings_sheet_sends_only_its_own_fields_on_done(page: Page, base_url: str) -> None:
    posted: list[dict[str, Any]] = []
    _settings_with_posts(page, base_url, posted)
    sheet = page.locator("#settingsSheet")

    # Close discards.
    page.locator('#settingsDialog [data-settings-sheet="hub"]').click()
    sheet.locator("input").nth(1).fill("e2e-model")
    surface(page, "settings.hub-sheet")
    page.locator("#settingsSheetClose").click()
    expect(sheet).to_be_hidden()
    assert posted == []

    # Done sends the sheet's two fields and nothing else.
    page.locator('#settingsDialog [data-settings-sheet="hub"]').click()
    sheet.locator("input").nth(1).fill("e2e-model")
    page.locator("#settingsSheetDone").click()
    expect(sheet).to_be_hidden()
    expect(page.locator("#toast")).to_contain_text("saved")
    assert len(posted) == 1
    assert sorted(posted[0]) == ["hub_base_url", "hub_model"]
    assert posted[0]["hub_model"] == "e2e-model"

    # The bot token is never prefilled, and a blank one is never sent.
    page.locator('#settingsDialog [data-settings-sheet="delivery"]').click()
    expect(sheet.locator("input[type=password]")).to_have_value("")
    surface(page, "settings.delivery-sheet")
    sheet.locator("input:not([type=password])").fill("12345")
    page.locator("#settingsSheetDone").click()
    expect(sheet).to_be_hidden()
    assert "telegram_bot_token" not in posted[1]
    assert sorted(posted[1]) == ["notifier", "telegram_chat_id"]


def test_a_rejected_setting_keeps_its_sheet_open(page: Page, base_url: str) -> None:
    posted: list[dict[str, Any]] = []
    _settings_with_posts(page, base_url, posted, reject="invalid classifier 'e2e'")
    page.locator('#settingsDialog [data-settings-sheet="hub"]').click()
    page.locator("#settingsSheet input").nth(1).fill("e2e-model")
    page.locator("#settingsSheetDone").click()
    expect(page.locator("#settingsSheetError")).to_contain_text("invalid classifier")
    expect(page.locator("#settingsSheet")).to_be_visible()
    expect(page.locator("#settingsSheet")).to_contain_text("Close discards your changes")
    surface(page, "settings.sheet-error")


def test_rebuild_confirms_in_a_sheet_not_a_native_dialog(page: Page, base_url: str) -> None:
    native: list[str] = []
    page.on("dialog", lambda d: (native.append(d.type), d.dismiss()))
    page.goto(base_url)
    page.wait_for_selector("#tabDashboard", state="attached")
    page.locator("#paneDashboard .home-settings").click()
    page.locator("#settingsDialog #execReprocess").click()
    sheet = page.locator("#settingsSheet")
    expect(sheet).to_be_visible()
    expect(sheet).to_contain_text("run and analysis history")
    expect(page.locator("#settingsSheetDone")).to_have_text("Rebuild cache")
    expect(page.locator("#settingsSheetDone")).to_have_class(re.compile(r"\bdanger\b"))
    surface(page, "settings.rebuild-confirm")
    # Cancelling queues nothing; nothing native ever opened.
    page.locator("#settingsSheetClose").click()
    expect(sheet).to_be_hidden()
    assert native == []


_ENROLL = {
    "configured": True, "rp_id": "e2e.example", "enrollment_open": True,
    "enrollment_seconds_left": 120,
    "devices": [{"id": "d-1", "label": "Test phone", "added_at": "2026-01-01"}],
}


@pytest.mark.parametrize("window_open", [True, False])
def test_passkey_enrollment_shows_while_its_window_is_open(
    page: Page, base_url: str, window_open: bool
) -> None:
    native: list[str] = []
    page.on("dialog", lambda d: (native.append(d.type), d.dismiss()))
    status = dict(_ENROLL, enrollment_open=window_open,
                  enrollment_seconds_left=120 if window_open else 0)
    page.route("**/api/webauthn/status", lambda route: route.fulfill(json=status))
    page.route("**/api/webauthn/devices/**", lambda route: route.abort())
    # Every source healthy, so the window is the most severe thing to say (an
    # offline source outranks it in both header lines).
    page.route("**/api/execution/health", lambda route: route.fulfill(json={"sources": []}))
    page.goto(base_url)
    page.wait_for_selector("#tabDashboard", state="attached")
    home = page.locator("#dashStatus")
    if not window_open:
        expect(home).not_to_have_text("Passkey enrollment open")
        page.locator("#paneDashboard .home-settings").click()
        expect(page.locator("#settingsStatus")).not_to_have_text("Passkey enrollment open")
        page.locator('#settingsDialog [data-settings-sheet="passkeys"]').click()
        expect(page.locator("#enrollDeviceBtn")).to_have_count(0)
        return
    # The window is announced on Home and in Settings, and Home's line opens it.
    expect(home).to_have_text("Passkey enrollment open")
    surface(page, "home.passkey-window")
    home.click()
    sheet = page.locator("#settingsSheet")
    expect(sheet).to_be_visible()
    expect(page.locator("#settingsStatus")).to_have_text("Passkey enrollment open")
    expect(page.locator("#enrollDeviceBtn")).to_be_visible()
    surface(page, "settings.passkeys-sheet")
    # Removal confirms inside the sheet; Cancel puts the row back.
    sheet.locator("#webauthnDevices .icon-button").click()
    expect(sheet.locator("#webauthnDevices")).to_contain_text("Remove \"Test phone\"?")
    surface(page, "settings.passkey-remove-confirm")
    sheet.locator("#webauthnDevices .button-ghost").click()
    expect(sheet.locator("#webauthnDevices .icon-button")).to_have_count(1)
    assert native == []
