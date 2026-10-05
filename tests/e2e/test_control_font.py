"""Every control renders in the page font, not the browser default (#343).

design rubric TYPE-02: a computed-style probe, controls must inherit the page
font. A `<button>`, `<input>`, `<select>` or `<textarea>` does not inherit
`font-family` by default (the user-agent sheet gives it Arial / system-ui), so
any app-local control style that forgets `font: inherit` renders in the wrong
face. The probe compares each visible control's computed `font-family` with the
body's, in each pane (the Chats history dialog included, where the per-message
actions live) and in the Settings dialog, in both themes and at phone width.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.smoke, pytest.mark.live_safe]

_TABS = ["Dashboard", "Chats", "Execution", "Audit", "Family"]

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


@pytest.mark.parametrize(
    ("width", "height", "theme"),
    [(1280, 900, "light"), (390, 844, "dark")],
    ids=["desktop-light", "phone-dark"],
)
def test_every_control_inherits_the_page_font(
    page: Page,
    base_url: str,
    scaled: Callable[[float], int],
    width: int,
    height: int,
    theme: str,
) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.goto(base_url)
    page.evaluate("t => { document.documentElement.dataset.theme = t }", theme)
    page.wait_for_selector("#tabDashboard", state="attached", timeout=scaled(10_000))

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
    offenders += [f"history: {o}" for o in _offenders(page, "#historyOverlay")]
    page.locator("#historyClose").click()

    page.locator(".pane:not([hidden]) .home-settings").click()
    offenders += [f"settings: {o}" for o in _offenders(page, "#settingsDialog")]

    assert not offenders, "controls not in the page font:\n" + "\n".join(sorted(set(offenders)))
