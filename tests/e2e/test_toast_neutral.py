"""App shell: the toast is the fleet's neutral frosted surface (#329).

design.md `toast`: one transient message, the nav bar's glass; only a real
error tints. A success/info toast used to carry a green border (`.toast.good`
-> `--on`). Drives the real `toast()` export and reads the computed style, so
a regression to a tinted or opaque-card success toast fails here.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from playwright.sync_api import Page

pytestmark = [pytest.mark.smoke, pytest.mark.live_safe]

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


def test_success_toast_is_neutral_frosted(
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


def test_error_toast_is_the_only_tinted_variant(
    page: Page, base_url: str, scaled: Callable[[float], int]
) -> None:
    error = _show(page, base_url, scaled, "error")
    assert error["live"] == "assertive"
    neutral = page.evaluate(_SHOW, ["Saved.", None])
    assert error["border"] != neutral["border"]
    assert error["background"] != neutral["background"]
