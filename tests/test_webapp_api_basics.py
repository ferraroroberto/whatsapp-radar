"""In-process FastAPI tests for the admin webapp surface.

The bearer + passkey gates key off the client IP, so each test picks a client
tuple: loopback (trusted), tailnet (passkey-allowed), or a public remote
(gated). Starlette's TestClient lets us set that via ``client=``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from starlette.testclient import TestClient

from app.webapp.server import create_app
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)
TAILNET = ("100.64.0.1", 5555)
REMOTE = ("203.0.113.5", 5555)


def _client(
    client: tuple[str, int] = LOOPBACK, *, token: str = "", password: str = ""
) -> TestClient:
    # Inject a clean config so the suite never depends on a developer's real
    # config/webapp_config.json (which may carry a token/password).
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token=token, auth_password=password)
    return TestClient(app, client=client)


@pytest.fixture
def loopback() -> Iterator[TestClient]:
    with _client() as c:
        yield c


# --- basic surface (loopback is trusted, bypasses both gates) --------------

def test_healthz(loopback: TestClient) -> None:
    r = loopback.get("/healthz")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_version_shape(loopback: TestClient) -> None:
    body = loopback.get("/api/version").json()
    assert {"git_sha", "built_at", "asset_hash"} <= set(body)
    assert body["asset_hash"]  # styles.css exists, so the hash is non-empty


def test_index_served_and_stamped(loopback: TestClient) -> None:
    r = loopback.get("/")
    assert r.status_code == 200
    assert "WhatsApp Radar" in r.text
    assert "?v=" in r.text  # asset URLs are version-stamped


def test_static_served(loopback: TestClient) -> None:
    r = loopback.get("/static/styles.css")
    assert r.status_code == 200


def test_index_revalidated(loopback: TestClient) -> None:
    # The iPhone-stale-index guard: GET / must force revalidation so Safari can't
    # keep referencing a ?v=<old hash> asset. Matches the fleet cache scheme.
    cc = loopback.get("/").headers.get("cache-control", "")
    assert "no-cache" in cc and "must-revalidate" in cc


def test_static_assets_immutable(loopback: TestClient) -> None:
    # Hashed JS/CSS get a year-long immutable cache — safe because the ?v= stamp
    # changes whenever the bytes change.
    for name in ("main.js", "styles.css"):
        cc = loopback.get(f"/static/{name}").headers.get("cache-control", "")
        assert "max-age=31536000" in cc and "immutable" in cc, name


def test_webauthn_status_default(loopback: TestClient) -> None:
    body = loopback.get("/api/webauthn/status").json()
    assert body["configured"] is False
    assert body["devices"] == []


# --- bearer-token gate ------------------------------------------------------

def test_bearer_blocks_remote_without_token() -> None:
    with _client(REMOTE, token="secret") as c:
        assert c.get("/api/version").status_code == 401
        ok = c.get("/api/version", headers={"Authorization": "Bearer secret"})
        assert ok.status_code == 200
        # Exempt paths stay open even with a token configured.
        assert c.get("/healthz").status_code == 200


def test_bearer_loopback_bypasses_token() -> None:
    with _client(LOOPBACK, token="secret") as c:
        assert c.get("/api/version").status_code == 200  # loopback never needs the token


# --- passkey ceremony gate (Tailscale-only over the network) ----------------

def test_webauthn_refused_for_public_remote() -> None:
    with _client(REMOTE) as c:
        assert c.get("/api/webauthn/status").status_code == 403


def test_webauthn_allowed_from_tailnet() -> None:
    with _client(TAILNET) as c:
        assert c.get("/api/webauthn/status").status_code == 200


def test_webauthn_refused_over_cloudflare() -> None:
    with _client(TAILNET) as c:
        r = c.get("/api/webauthn/status", headers={"cf-ray": "abc", "cf-connecting-ip": "1.2.3.4"})
        assert r.status_code == 403


# --- password login ---------------------------------------------------------

def test_login_unconfigured_returns_503(loopback: TestClient) -> None:
    assert loopback.post("/api/login", json={"password": "x"}).status_code == 503


def test_login_success_and_failure() -> None:
    with _client(REMOTE, token="thetoken", password="hunter2") as c:
        ok = c.post("/api/login", json={"password": "hunter2"})
        assert ok.status_code == 200
        assert ok.json()["token"] == "thetoken"
        assert c.post("/api/login", json={"password": "wrong"}).status_code == 401
