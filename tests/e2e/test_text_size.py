"""Settings: the vendored text-size control scales every view (#334).

design.md "Layout" -> "Text size": a persisted Small / Default / Large setting
scales the root font-size (112.5% at Large), so every rem-based type role grows
with it. The probe measures the computed font-size of every text-bearing
element in each pane and in the Settings dialog at Default and at Large and
fails on any element that does not grow by exactly the Large step, which is how
a px font-size hiding in the app's own CSS shows up.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from playwright.sync_api import Locator, Page, expect

pytestmark = [pytest.mark.smoke, pytest.mark.live_safe]

_LARGE_RATIO = 1.125
_TABS = ["Dashboard", "Chats", "Execution", "Audit", "Family"]

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
        const cls = typeof el.className === 'string' ? el.className.trim().split(/\s+/)[0] : '';
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
    page.locator(f"#tab{name}").click()
    return f"#pane{name}"


def _measure_all(page: Page) -> dict[str, list[list]]:
    sizes: dict[str, list[list]] = {}
    for name in [*_TABS, None]:
        root = _open(page, name)
        sizes[root] = page.evaluate(_MEASURE, root)
        if name is None:
            page.locator("#settingsClose").click()
    return sizes


@pytest.mark.parametrize(
    ("width", "height", "theme"),
    [(1280, 900, "light"), (390, 844, "dark")],
    ids=["desktop-light", "phone-dark"],
)
def test_large_scales_every_view(
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
    _measure_all(page)  # warm-up: lazy panes fetch on first visit, so the DOM settles
    page.wait_for_timeout(scaled(1_000))
    default = _measure_all(page)

    # Switch through the real control, in Settings.
    _gear(page).click()
    page.locator('#textSizeControl [data-textsize="large"]').click()
    expect(page.locator("html")).to_have_attribute("data-textsize", "large")
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


def test_text_size_persists_across_reload(
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
