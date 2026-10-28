"""ETag + 304 on the entry document.

A phone revalidates ``/`` on every launch (``no-cache``); a matching validator
must answer a body-less 304, and the validator must change whenever the build
does, including a nested module the stamped HTML never names.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.webapp.routers import misc
from app.webapp.routers._helpers import STATIC_DIR
from app.webapp.server import create_app
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)


def _client(asset_hashes: dict[str, str] | None = None) -> TestClient:
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    if asset_hashes is not None:
        app.state.asset_hashes = asset_hashes
    return TestClient(app, client=LOOPBACK)


def _etag(client: TestClient) -> str:
    return client.get("/").headers["etag"]


def test_index_sends_a_weak_etag_and_still_revalidates() -> None:
    with _client() as client:
        response = client.get("/")
    assert response.headers["etag"].startswith('W/"')
    cache_control = response.headers["cache-control"]
    assert "no-cache" in cache_control and "must-revalidate" in cache_control


@pytest.mark.parametrize("wrap", ["{weak}", "{strong}", '"x", {weak}', "*"])
def test_matching_if_none_match_gets_a_bodyless_304(wrap: str) -> None:
    with _client() as client:
        etag = _etag(client)
        header = wrap.format(weak=etag, strong=etag.removeprefix("W/"))
        response = client.get("/", headers={"If-None-Match": header})
    assert response.status_code == 304
    assert response.content == b""
    assert response.headers["etag"] == etag
    assert "no-cache" in response.headers["cache-control"]


def test_stale_if_none_match_gets_the_full_document() -> None:
    with _client() as client:
        response = client.get("/", headers={"If-None-Match": 'W/"stale"'})
    assert response.status_code == 200
    assert "<title>" in response.text


def test_304_survives_a_gzip_client() -> None:
    with _client() as client:
        etag = _etag(client)
        response = client.get("/", headers={"If-None-Match": etag, "Accept-Encoding": "gzip"})
    assert response.status_code == 304
    assert "content-encoding" not in response.headers


def test_etag_changes_with_the_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    with _client() as client:
        before = _etag(client)
        monkeypatch.setattr(misc, "_GIT_SHA", "0000000")
        assert _etag(client) != before


def test_etag_changes_when_only_a_nested_module_changes() -> None:
    # A hash for a file the index never names leaves the stamped HTML identical,
    # so only the build fingerprint can tell the two builds apart.
    with _client({"nested/deep.js": "aaaaaaaa"}) as first:
        before = first.get("/")
    with _client({"nested/deep.js": "bbbbbbbb"}) as second:
        after = second.get("/")
    assert before.text == after.text
    assert before.headers["etag"] != after.headers["etag"]


def test_etag_changes_when_index_html_is_edited(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    static = tmp_path / "static"
    shutil.copytree(STATIC_DIR, static)
    monkeypatch.setattr(misc, "STATIC_DIR", static)
    with _client() as client:
        before = _etag(client)
        index = static / "index.html"
        index.write_text(index.read_text(encoding="utf-8") + "<!-- edited -->\n", encoding="utf-8")
        assert _etag(client) != before
