"""App shell: iOS status-bar safe-area regression (#188).

In standalone (installed-PWA) mode on iPhone, page content rendered under the
iOS status bar — the open Rules card's title collided with the system clock.
Root cause: `styles.css`'s own unconditional `.app { padding: ... }` shorthand
has the same selector specificity as, and loads after, the vendored
`nav-tabs.css`'s dedicated coarse-pointer/narrow-viewport `.app` padding rule
— so the app-owned rule silently won the cascade and clobbered *both* the
vendored top safe-area cushion (`env(safe-area-inset-top) + gap`) and the
bottom floating-tab-bar clearance (`env(safe-area-inset-bottom) + ~103px`),
leaving only a bare `env(safe-area-inset-top, 0)` on top with no headroom.
Mirrors app-launcher issue #355, which fixed the same clobbering for the
bottom edge only; this fix extends the split to the top edge too.

Playwright/CI browsers report `env(safe-area-inset-*)` as 0 (no real notch
hardware), so this can't assert the literal on-device pixel gap. Instead it
asserts the *cascade winner*: on a narrow, coarse-pointer (phone-shaped)
viewport the vendored nav-tabs.css formula must own `.app`'s top/bottom
padding (proven by the extra `--gap` / bottom-tab-bar terms showing up in the
computed style, which the old app-owned shorthand could never produce), and
on a wide/fine-pointer (desktop/browser-tab) viewport the app-owned padding
must be byte-identical to before the fix (0px extra top, 24px bottom) — no
layout shift for non-mobile users.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from playwright.sync_api import Browser, Page, Playwright

pytestmark = pytest.mark.smoke


def _app_padding(page: Page) -> dict[str, str]:
    return page.evaluate(
        """() => {
            const app = document.querySelector('.app');
            const cs = getComputedStyle(app);
            return { top: cs.paddingTop, bottom: cs.paddingBottom };
        }"""
    )


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_mobile_shell_defers_to_vendored_safe_area(
    playwright: Playwright,
    browser: Browser,
    base_url: str,
    color_scheme: str,
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
    device["color_scheme"] = color_scheme
    context = browser.new_context(**device)
    page = context.new_page()
    page.set_default_timeout(scaled(30_000))
    try:
        page.goto(base_url)
        page.wait_for_selector("#tabDashboard", state="attached", timeout=scaled(10_000))

        padding = _app_padding(page)
    finally:
        context.close()
    # env(safe-area-inset-top, 0) resolves to 0 in every headless/CI browser
    # (no real notch), so a non-zero top means the vendored `+ var(--gap)`
    # term survived — proof nav-tabs.css's rule won, not the app-owned one.
    assert padding["top"] == "12px", (
        f"expected the vendored top-safe-area formula (env(0) + --gap=12px) to win "
        f"on a narrow coarse-pointer viewport, got padding-top={padding['top']!r}"
    )
    # 115px = env(bottom, 0) + --bottom-tabs-margin(21) + --bottom-tabs-height(61)
    # + --bottom-tabs-margin(21) + --gap(12) — the full floating-pill clearance.
    assert padding["bottom"] == "115px", (
        f"expected the vendored bottom floating-tab-bar clearance to win on a "
        f"narrow coarse-pointer viewport, got padding-bottom={padding['bottom']!r}"
    )


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_desktop_browser_tab_shell_padding_unchanged(
    page: Page,
    base_url: str,
    color_scheme: str,
    scaled: Callable[[float], int],
) -> None:
    """Wide viewport (browser-tab / desktop): app-owned padding, no shift."""
    page.emulate_media(color_scheme=color_scheme)
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(base_url)
    page.wait_for_selector("#tabDashboard", state="attached", timeout=scaled(10_000))

    padding = _app_padding(page)
    # Byte-identical to pre-fix: env(safe-area-inset-*, 0) resolves to 0 in
    # CI, so top is exactly 0 and bottom is exactly --space-lg (24px) — the
    # app-owned desktop formula, completely untouched by the #188 change.
    assert padding["top"] == "0px", (
        f"desktop/browser-tab top padding shifted: {padding['top']!r} (expected 0px)"
    )
    assert padding["bottom"] == "24px", (
        f"desktop/browser-tab bottom padding shifted: {padding['bottom']!r} (expected 24px)"
    )
