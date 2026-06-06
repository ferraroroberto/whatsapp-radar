"""WebKit-projection pin for the iPhone index-revalidation header.

iOS Safari (especially PWA-installed) will serve a stale ``index.html`` and
request a ``?v=<old hash>`` asset that no longer exists unless the index
response carries ``Cache-Control: no-cache, must-revalidate``. The non-browser
``test_cache_busting`` pins the header at the HTTP level; this one runs through
the real WebKit network stack so a WebKit-specific regression (or a middleware
ordering bug that strips the header) surfaces here.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, Response

pytestmark = pytest.mark.smoke


def test_index_cache_control_visible_to_webkit(
    page: Page, base_url: str, browser_name: str
) -> None:
    if browser_name != "webkit":
        pytest.skip("WebKit projection only (iOS Safari is the original regression)")

    captured: dict[str, object] = {}

    def _on_response(res: Response) -> None:
        if "cache-control" in captured:
            return
        url = res.url.rstrip("/")
        if url == base_url.rstrip("/"):
            captured["cache-control"] = res.headers.get("cache-control", "")
            captured["status"] = res.status

    page.on("response", _on_response)
    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    page.wait_for_selector("#tabDashboard", state="attached", timeout=5_000)

    assert captured.get("status") == 200, (
        f"GET / returned {captured.get('status')!r} under WebKit"
    )
    cc = str(captured.get("cache-control", ""))
    assert "no-cache" in cc and "must-revalidate" in cc, (
        f"WebKit saw Cache-Control={cc!r} on /; the iPhone-stale-index fix regressed "
        "or was stripped by middleware ordering"
    )
