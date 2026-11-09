"""Design conformance probes over the whole shell: type, safe area and toast.

Computed-style probes that each walk a set of views rather than one flow:

* every visible control inherits the page font (a `<button>`, `<input>`,
  `<select>` or `<textarea>` does not by default, so any app-local control style
  that forgets `font: inherit` renders in the wrong face), in each pane, the
  Chats history dialog, the Family and Settings sheets, in both themes;
* the persisted Large text size scales every text-bearing element by exactly the
  Large step, which is how a px font-size hiding in the app's own CSS shows up,
  and survives a reload;
* the phone-shaped shell lets the vendored nav own the safe-area padding while
  the desktop shell keeps its own;
* the toast is the neutral frosted surface and only an error tints.

One test, each probe a fresh visit.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from playwright.sync_api import Browser, Locator, Page, Playwright, expect

from tests.e2e._audit_runs import reset_page
from tests.e2e._surface import surface

pytestmark = [pytest.mark.smoke, pytest.mark.live_safe]

_TABS = ["Dashboard", "Activity", "Chats", "Family"]
# (width, height, theme): a wide light visit and a phone dark one.
_VISITS = [(1280, 900, "light"), (390, 844, "dark")]

# Visible controls under `rootSel` whose computed font-family is not the body's.
_OFFENDERS = """([rootSel, bodyFamily]) => {
    const out = [];
    for (const el of document.querySelectorAll(
        rootSel + ' button, ' + rootSel + ' input, ' + rootSel + ' select, '
        + rootSel + ' textarea')) {
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') continue;
        if (cs.fontFamily === bodyFamily) continue;
        const cls = typeof el.className === 'string' ? el.className.trim().split(/\\s+/)[0] : '';
        out.push(el.tagName.toLowerCase() + (cls ? '.' + cls : '') + ' -> ' + cs.fontFamily);
    }
    return out;
}"""


def _offenders(page: Page, root: str) -> list[str]:
    body_family = page.evaluate("getComputedStyle(document.body).fontFamily")
    return page.evaluate(_OFFENDERS, [root, body_family])


def _every_control_inherits_the_page_font(page: Page, scaled: Callable[[float], int]) -> None:
    offenders: list[str] = []
    for name in _TABS:
        page.locator(f"#tab{name}").click()
        page.wait_for_timeout(scaled(500))  # lazy panes fetch on first visit
        offenders += [f"#pane{name}: {o}" for o in _offenders(page, f"#pane{name}")]

    # Chats: the per-chat history dialog hosts the per-message action buttons.
    page.locator("#tabChats").click()
    page.locator("#paneChats .chat-main").first.click()
    page.wait_for_selector("#historyClose", state="visible", timeout=scaled(10_000))
    page.wait_for_timeout(scaled(500))
    offenders += [f"history: {o}" for o in _offenders(page, "#historyCard")]
    page.locator("#historyClose").click()

    # Family: the rule sheets host the 44px inputs and pickers (#376).
    page.locator("#tabFamily").click()
    expect(page.locator('#paneFamily [data-value="duty"]')).not_to_be_empty()
    for opener, root, close in (
        ('[data-sheet="duty"]', "#familySheet", "#familySheetClose"),
        ("#familyTravelRow", "#familyTravelSheet", "#familyTravelClose"),
    ):
        page.locator(f"#paneFamily {opener}").click()
        page.wait_for_selector(root, state="visible", timeout=scaled(10_000))
        offenders += [f"{root}: {o}" for o in _offenders(page, root)]
        page.locator(close).click()

    page.locator(".pane:not([hidden]) .home-settings").click()
    offenders += [f"settings: {o}" for o in _offenders(page, "#settingsDialog")]
    # Settings' sheets host its 44px fields (#377).
    for key, root in (("delivery", "#settingsSheet"), ("source-whatsapp", "#sourceSheet")):
        page.locator(f'#settingsDialog [data-settings-sheet="{key}"]').click()
        page.wait_for_selector(root, state="visible", timeout=scaled(10_000))
        offenders += [f"{root}: {o}" for o in _offenders(page, root)]
        page.keyboard.press("Escape")

    assert not offenders, "controls not in the page font:\n" + "\n".join(sorted(set(offenders)))


_LARGE_RATIO = 1.125
# The week sheet: a Family rule sheet with its 44px pickers (#376).
_FAMILY_SHEET = "FamilySheet"
# Settings' Delivery sheet: its 44px fields (#377).
_SETTINGS_SHEET = "SettingsSheet"

# Computed font-size of every visible element that carries its own text, in
# document order. Closed <details> are opened so their content is measured too.
_MEASURE = """(rootSel) => {
    const root = document.querySelector(rootSel);
    root.querySelectorAll('details').forEach((d) => { d.open = true; });
    const out = [];
    for (const el of root.querySelectorAll('*')) {
        if (['SCRIPT', 'STYLE', 'svg', 'path', 'use'].includes(el.tagName)) continue;
        const own = Array.from(el.childNodes).some(
            (n) => n.nodeType === 3 && n.textContent.trim() !== '');
        const cs = getComputedStyle(el);
        if (!own || cs.display === 'none' || cs.visibility === 'hidden') continue;
        const cls = typeof el.className === 'string' ? el.className.trim().split(/\\s+/)[0] : '';
        out.push([el.tagName.toLowerCase() + (cls ? '.' + cls : ''), parseFloat(cs.fontSize)]);
    }
    return out;
}"""


def _gear(page: Page) -> Locator:
    """The Settings gear of whichever pane is showing."""
    return page.locator(".pane:not([hidden]) .home-settings")


def _open(page: Page, name: str | None) -> str:
    """Show the pane (or the Settings dialog when ``name`` is None); return its selector."""
    if name is None:
        _gear(page).click()
        return "#settingsDialog"
    if name == _FAMILY_SHEET:
        page.locator("#tabFamily").click()
        expect(page.locator('#paneFamily [data-value="duty"]')).not_to_be_empty()
        page.locator('#paneFamily [data-sheet="duty"]').click()
        expect(page.locator("#familySheet")).to_be_visible()
        return "#familySheet"
    if name == _SETTINGS_SHEET:
        _gear(page).click()
        expect(page.locator('#settingsDialog [data-value="delivery"]')).not_to_be_empty()
        page.locator('#settingsDialog [data-settings-sheet="delivery"]').click()
        expect(page.locator("#settingsSheet")).to_be_visible()
        return "#settingsSheet"
    page.locator(f"#tab{name}").click()
    return f"#pane{name}"


def _measure_all(page: Page) -> dict[str, list[list]]:
    sizes: dict[str, list[list]] = {}
    for name in [*_TABS, _FAMILY_SHEET, _SETTINGS_SHEET, None]:
        root = _open(page, name)
        sizes[root] = page.evaluate(_MEASURE, root)
        if name is None:
            page.locator("#settingsClose").click()
        elif name == _FAMILY_SHEET:
            page.locator("#familySheetClose").click()
        elif name == _SETTINGS_SHEET:
            page.locator("#settingsSheetClose").click()
            page.locator("#settingsClose").click()
    return sizes


def _large_scales_every_view(page: Page, scaled: Callable[[float], int]) -> None:
    _measure_all(page)  # warm-up: lazy panes fetch on first visit, so the DOM settles
    page.wait_for_timeout(scaled(1_000))
    default = _measure_all(page)

    # Switch through the real control, in Settings. The segmented track is a
    # fixed control height, so the Large step grows its labels, not the track.
    _gear(page).click()
    control = page.locator("#textSizeControl")
    track_before = control.bounding_box()
    page.locator('#textSizeControl [data-textsize="large"]').click()
    expect(page.locator("html")).to_have_attribute("data-textsize", "large")
    expect(page.locator('#textSizeControl [data-textsize="large"]')).to_have_attribute(
        "aria-pressed", "true"
    )
    track_after = control.bounding_box()
    assert track_before and track_after
    assert track_after["height"] == track_before["height"], "Large reflowed the text-size track"
    surface(page, "settings.text-size-large")
    page.locator("#settingsClose").click()
    large = _measure_all(page)

    offenders = []
    for root, base in default.items():
        assert base, f"{root}: nothing measured, the probe is blind here"
        assert len(base) == len(large[root]), f"{root}: DOM changed between measurements"
        for (name, small_px), (_, large_px) in zip(base, large[root], strict=True):
            if abs(large_px / small_px - _LARGE_RATIO) > 0.01:
                offenders.append(f"{root} {name}: {small_px}px -> {large_px}px")
    assert not offenders, "text that does not scale with Large:\n" + "\n".join(
        sorted(set(offenders))
    )


def _text_size_persists_across_reload(
    page: Page, base_url: str, scaled: Callable[[float], int]
) -> None:
    page.goto(base_url)
    page.wait_for_selector("#tabDashboard", state="attached", timeout=scaled(10_000))
    _gear(page).click()
    page.locator('#textSizeControl [data-textsize="small"]').click()
    expect(page.locator("html")).to_have_attribute("data-textsize", "small")
    page.reload()
    # Stamped by the inline boot script before first paint, and the control
    # reads it back as the active step.
    expect(page.locator("html")).to_have_attribute("data-textsize", "small")
    _gear(page).click()
    expect(page.locator('#textSizeControl [data-textsize="small"]')).to_have_attribute(
        "aria-pressed", "true"
    )


def _app_padding(page: Page) -> dict[str, str]:
    return page.evaluate(
        """() => {
            const app = document.querySelector('.app');
            const cs = getComputedStyle(app);
            return { top: cs.paddingTop, bottom: cs.paddingBottom };
        }"""
    )


def _mobile_shell_defers_to_vendored_safe_area(
    playwright: Playwright,
    browser: Browser,
    base_url: str,
    scaled: Callable[[float], int],
) -> None:
    """Narrow + coarse pointer (phone shape): nav-tabs.css owns .app padding."""
    # has_touch/is_mobile are context-creation-time-only options (they drive
    # the `pointer: coarse` media feature both nav-tabs.css and the app-owned
    # rule gate on), so build a fresh phone-shaped context rather than
    # reusing the injected `page` fixture's desktop default — same approach
    # as test_family_layout.py's iPhone-device context.
    device = dict(playwright.devices["iPhone 13"])
    device["viewport"] = {"width": 390, "height": 844}
    context = browser.new_context(**device)
    page = context.new_page()
    page.set_default_timeout(scaled(30_000))
    try:
        page.goto(base_url)
        page.wait_for_selector("#tabDashboard", state="attached", timeout=scaled(10_000))

        padding = _app_padding(page)
    finally:
        context.close()
    # The vendored nav's phone top padding is `env(safe-area-inset-top)` alone
    # (the top fade is off at scroll-top), and env() resolves to 0 in every
    # headless/CI browser (no real notch), so the top edge is 0px here. The
    # bottom-clearance assertion below discriminates the cascade winner.
    assert padding["top"] == "0px", (
        f"expected the vendored top-safe-area formula (env(0)) on a "
        f"narrow coarse-pointer viewport, got padding-top={padding['top']!r}"
    )
    # 118px = env(bottom, 0) + max(--nav-fade-tail(24), --bottom-tabs-margin(21))
    # + --bottom-tabs-height(61) + --bottom-tabs-margin(21) + --gap(12) — the
    # full bottom-fade reserve.
    assert padding["bottom"] == "118px", (
        f"expected the vendored bottom floating-tab-bar clearance to win on a "
        f"narrow coarse-pointer viewport, got padding-bottom={padding['bottom']!r}"
    )


def _desktop_browser_tab_shell_padding_unchanged(
    page: Page,
    base_url: str,
    scaled: Callable[[float], int],
) -> None:
    """Wide viewport (browser-tab / desktop): the vendored wide-layout rail owns the top gap."""
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(base_url)
    page.wait_for_selector("#tabDashboard", state="attached", timeout=scaled(10_000))

    padding = _app_padding(page)
    # env(safe-area-inset-*, 0) resolves to 0 in CI. At 1280px + fine pointer
    # nav-tabs.css's wide-layout rail (design.md Layout "Wide layout") replaces
    # the sticky top control, so it supplies the top gap itself:
    # env(0) + --gap(12px) + --space-xs(4px) = 16px. The bottom is still the
    # app-owned desktop formula, exactly --space-lg (24px), untouched by #188.
    assert padding["top"] == "16px", (
        f"desktop/browser-tab top padding shifted: {padding['top']!r} (expected 16px)"
    )
    assert padding["bottom"] == "24px", (
        f"desktop/browser-tab bottom padding shifted: {padding['bottom']!r} (expected 24px)"
    )


_SHOW = """async ([message, kind]) => {
    const { toast } = await import('/static/api.js');
    toast(message, kind);
    const el = document.getElementById('toast');
    const cs = getComputedStyle(el);
    return {
        hidden: el.hidden,
        border: cs.borderTopColor,
        background: cs.backgroundColor,
        blur: cs.backdropFilter || cs.webkitBackdropFilter || 'none',
        live: el.getAttribute('aria-live'),
        on: getComputedStyle(document.documentElement).getPropertyValue('--on').trim(),
    };
}"""


def _show(page: Page, base_url: str, scaled: Callable[[float], int], kind: str | None) -> dict:
    page.goto(base_url)
    page.wait_for_selector("#tabDashboard", state="attached", timeout=scaled(10_000))
    return page.evaluate(_SHOW, ["Saved.", kind])


def _success_toast_is_neutral_frosted(
    page: Page, base_url: str, scaled: Callable[[float], int]
) -> None:
    neutral = _show(page, base_url, scaled, None)
    assert neutral["hidden"] is False
    assert "blur" in neutral["blur"], f"toast lost the frosted glass: {neutral['blur']!r}"
    assert neutral["live"] == "polite"
    # The neutral border is the nav's hairline, never the success green: a
    # green toast is the pre-#329 `.toast.good` tint.
    assert neutral["border"] != "rgb(26, 127, 55)", neutral["border"]
    assert neutral["border"] != "rgb(63, 185, 80)", neutral["border"]
    surface(page, "toast.neutral")


def _error_toast_is_the_only_tinted_variant(
    page: Page, base_url: str, scaled: Callable[[float], int]
) -> None:
    error = _show(page, base_url, scaled, "error")
    assert error["live"] == "assertive"
    surface(page, "toast.error")
    neutral = page.evaluate(_SHOW, ["Saved.", None])
    assert error["border"] != neutral["border"]
    assert error["background"] != neutral["background"]


def _visit(
    page: Page, base_url: str, scaled: Callable[[float], int], width: int, height: int, theme: str
) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.goto(base_url)
    page.evaluate("t => { document.documentElement.dataset.theme = t }", theme)
    page.wait_for_selector("#tabDashboard", state="attached", timeout=scaled(10_000))


def test_controls_keep_the_page_font_text_scales_and_shell_chrome_holds(
    playwright: Playwright,
    browser: Browser,
    page: Page,
    base_url: str,
    scaled: Callable[[float], int],
) -> None:
    for width, height, theme in _VISITS:
        _visit(page, base_url, scaled, width, height, theme)
        _every_control_inherits_the_page_font(page, scaled)
        reset_page(page)
        _visit(page, base_url, scaled, width, height, theme)
        _large_scales_every_view(page, scaled)
        reset_page(page)
    _text_size_persists_across_reload(page, base_url, scaled)
    reset_page(page)
    _mobile_shell_defers_to_vendored_safe_area(playwright, browser, base_url, scaled)
    _desktop_browser_tab_shell_padding_unchanged(page, base_url, scaled)
    _success_toast_is_neutral_frosted(page, base_url, scaled)
    _error_toast_is_the_only_tinted_variant(page, base_url, scaled)
