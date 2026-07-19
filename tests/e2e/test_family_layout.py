"""Family tab: childcare-window layout regression (#187).

#175 rendered each childcare window as a bordered sub-card nested inside the
Rules card and its Start/End time row overflowed past the right edge at
390px on a real iPhone (WebKit is the native-control regression surface —
Chromium's desktop-style `<input type=time>` doesn't reproduce it, so this
is pinned WebKit-only, matching test_iphone_revalidate.py's precedent).
#187 flattens the sub-cards to flat list-row content (design.md's list-row
contract: full-bleed rows on a top hairline after the first, never a nested
canvas-subtle card) and gives Start/End a constrained two-column grid.

Privacy: the autobooted app receives a disposable sanitized local-config
fixture, including its Family rules. This test only adds blank windows via the
pure client-side draft mutation (see family.js), and its assertions remain
structural only (classes, computed styles, bounding boxes) — never rule
content.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from playwright.sync_api import Browser, Playwright

pytestmark = pytest.mark.smoke


def _open_family_with_two_new_windows(page, base_url: str, scaled: Callable[[float], int]) -> None:
    page.goto(base_url)
    page.wait_for_selector("#tabFamily", state="attached")
    page.locator("#tabFamily").click()
    page.wait_for_selector("#paneFamily", state="visible")

    card = page.locator("#familyRulesCard")
    if card.get_attribute("open") is None:
        page.locator("#familyRulesCard summary").click()
    page.wait_for_timeout(scaled(200))

    add_btn = page.locator("#familyEditable .ghost-btn", has_text="Add childcare window")
    add_btn.click()
    add_btn.click()
    page.wait_for_timeout(scaled(200))


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_childcare_windows_flat_and_fit_390px(
    playwright: Playwright,
    browser: Browser,
    base_url: str,
    browser_name: str,
    color_scheme: str,
    scaled: Callable[[float], int],
) -> None:
    if browser_name != "webkit":
        pytest.skip("WebKit projection only (iOS Safari's native time-input is the regression)")

    device = dict(playwright.devices["iPhone 13"])
    device["viewport"] = {"width": 390, "height": 844}
    device["color_scheme"] = color_scheme
    context = browser.new_context(**device)
    page = context.new_page()
    page.set_default_timeout(scaled(30_000))
    try:
        _open_family_with_two_new_windows(page, base_url, scaled)

        windows = page.locator(".family-window")
        count = windows.count()
        assert count >= 2, "expected at least the two freshly-added windows"

        # 1. Flatten: no nested-card border on the row itself; a hairline
        # divider (border-top) separates every window after the first.
        first_border = page.evaluate(
            "(el) => getComputedStyle(el).borderTopStyle", windows.nth(0).element_handle()
        )
        assert first_border == "none", f"first window should have no border, got {first_border!r}"
        second_divider = page.evaluate(
            "(el) => getComputedStyle(el).borderTopStyle", windows.nth(1).element_handle()
        )
        assert second_divider == "solid", (
            f"second window should have a hairline top divider, got {second_divider!r}"
        )

        # 2. Zero horizontal overflow anywhere in the Family pane at 390px.
        overflowing = page.evaluate(
            """
            () => {
              const vw = window.innerWidth;
              const bad = [];
              document.querySelectorAll('#paneFamily *').forEach((el) => {
                const r = el.getBoundingClientRect();
                if (r.width > 0 && r.right > vw + 0.5) {
                  bad.push({tag: el.tagName, cls: String(el.className), right: r.right});
                }
              });
              return bad;
            }
            """
        )
        assert overflowing == [], f"horizontal overflow at 390px: {overflowing}"
        assert page.evaluate("document.documentElement.scrollWidth") == 390

        # 3. Start/End render as a real two-column grid (not stacked) and
        # both stay within the viewport.
        times = windows.nth(0).locator(".family-window-times")
        time_inputs = times.locator("input[type=time]")
        assert time_inputs.count() == 2
        start_box = time_inputs.nth(0).bounding_box()
        end_box = time_inputs.nth(1).bounding_box()
        assert start_box is not None and end_box is not None
        assert end_box["x"] > start_box["x"] + start_box["width"] - 1, (
            "Start/End should sit side by side in a two-column grid"
        )
        assert end_box["x"] + end_box["width"] <= 390 + 0.5, (
            f"End input overflows the 390px viewport: {end_box}"
        )

        # 4. Bottom clearance: scrolled to the document's true end, "+ Add
        # childcare window" clears the floating nav pill (no overlap).
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(scaled(200))
        add_box = page.locator(
            "#familyEditable .ghost-btn", has_text="Add childcare window"
        ).bounding_box()
        nav_box = page.locator(".tabs").bounding_box()
        assert add_box is not None and nav_box is not None
        assert add_box["y"] + add_box["height"] <= nav_box["y"] + 0.5, (
            f"add-window button ({add_box}) overlaps the floating nav pill ({nav_box})"
        )
    finally:
        context.close()
