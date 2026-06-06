"""Cache hygiene regression net (fleet standard, ported from App Launcher).

The four invariants pinned here mirror App Launcher's and local-llm-hub's
identical static-serving scheme:

1. ``/`` is always revalidated (Safari, especially PWA-installed, otherwise
   serves a stale ``index.html`` that references a ``?v=<old hash>`` asset which
   no longer exists).
2. ``/static/*.{css,js}`` is immutable for a year so the ``?v=`` bust above
   actually pays off (no re-download until the hash changes).
3. The ``?v=<hash>`` stamped into the served ``index.html`` matches
   ``compute_asset_hashes(STATIC_DIR)`` — *this* is the check that catches
   "edited a JS/CSS file but the running webapp serves an older hash" (i.e. the
   tray needs a restart), which is exactly the staleness symptom on the phone.
4. ``/api/version`` returns the three keys the Settings build-line + tray Status
   rely on.

Non-browser: uses ``requests`` against the live/auto-booted server. The checks
are server-side, so the file runs once on the chromium projection and skips the
webkit duplicate.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import requests

from src.static_versioning import compute_asset_hashes

pytestmark = pytest.mark.smoke

_STATIC_DIR = Path(__file__).resolve().parents[2] / "app" / "webapp" / "static"
_INDEX_HREF_RE = re.compile(
    r"""(?:href|src)=['"]/static/(?P<name>[\w\-.]+\.(?:css|js))\?v=(?P<hash>[a-f0-9]+)['"]"""
)


@pytest.fixture(scope="session", autouse=True)
def _run_once(browser_name: str) -> None:
    if browser_name != "chromium":
        pytest.skip("server-side check; runs once on the chromium projection")


def test_index_is_revalidated(base_url: str) -> None:
    res = requests.get(f"{base_url}/", verify=False, timeout=5)  # noqa: S501
    res.raise_for_status()
    cc = res.headers.get("Cache-Control", "")
    assert "no-cache" in cc and "must-revalidate" in cc, (
        f"GET / must force revalidation; got Cache-Control={cc!r}"
    )


def test_static_assets_are_immutable(base_url: str) -> None:
    asset_hashes = compute_asset_hashes(_STATIC_DIR)
    assert asset_hashes, "no hashable assets found under app/webapp/static"
    name = "main.js"
    stamp = asset_hashes[name]
    res = requests.get(f"{base_url}/static/{name}?v={stamp}", verify=False, timeout=5)  # noqa: S501
    res.raise_for_status()
    cc = res.headers.get("Cache-Control", "")
    assert "immutable" in cc and "max-age=31536000" in cc, (
        f"GET /static/{name} must be immutable for a year; got Cache-Control={cc!r}"
    )


def test_served_index_hashes_match_disk(base_url: str) -> None:
    """The single check that catches 'edited an asset but the webapp is stale'."""
    res = requests.get(f"{base_url}/", verify=False, timeout=5)  # noqa: S501
    res.raise_for_status()
    served = {
        m.group("name"): m.group("hash") for m in _INDEX_HREF_RE.finditer(res.text)
    }
    assert served, "no hashed /static/*.{css,js} references found in served index.html"
    on_disk = compute_asset_hashes(_STATIC_DIR)
    for name, stamp in served.items():
        expected = on_disk.get(name)
        assert expected is not None, f"served index references {name} but it isn't on disk"
        assert stamp == expected, (
            f"{name}: served stamp {stamp!r} != fleet hash {expected!r} — the webapp's "
            "asset_hashes was computed against different bytes (tray needs restart, or a "
            "file changed under the running process)"
        )


def test_api_version_shape(base_url: str) -> None:
    res = requests.get(f"{base_url}/api/version", verify=False, timeout=5)  # noqa: S501
    res.raise_for_status()
    body = res.json()
    for key in ("git_sha", "built_at", "asset_hash"):
        assert key in body, f"/api/version missing key {key!r}: {body}"
        assert isinstance(body[key], str), f"/api/version[{key}] is not a string: {body[key]!r}"
    assert body["git_sha"], "/api/version.git_sha is empty"
    assert body["built_at"], "/api/version.built_at is empty"
