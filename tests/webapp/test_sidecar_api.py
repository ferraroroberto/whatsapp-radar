"""Sidecar router (/api/sidecar): status, relaunch, and QR serving.

Loopback client (bypasses the bearer gate) pointed at a throwaway buffer dir via
``app.state.linked_device_dir`` — so status is derived from a hand-written
``status.json`` and the spawn layer is stubbed; no Node is ever launched.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from src.connector import sidecar as sidecar_mod
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    from app.webapp.server import create_app

    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.linked_device_dir = tmp_path
    with TestClient(app, client=LOOPBACK) as c:
        yield c


def _write_status(buffer_dir: Path, **fields: Any) -> None:
    (buffer_dir / "status.json").write_text(json.dumps(fields), encoding="utf-8")


def test_status_reports_stopped_for_empty_buffer(client: TestClient) -> None:
    body = client.get("/api/sidecar/status").json()
    assert body["state"] == "stopped"
    assert body["is_live"] is False
    assert set(body) >= {"state", "detail", "paired", "connected", "has_qr", "is_relaunchable"}


def test_status_reports_running(client: TestClient, tmp_path: Path) -> None:
    _write_status(
        tmp_path, paired=True, connected=True, last_update=datetime.now(UTC).isoformat(),
        chats=4, messages=8,
    )
    body = client.get("/api/sidecar/status").json()
    assert body["state"] == "running"
    assert body["is_live"] is True


def test_start_launches_via_supervisor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: dict[str, Any] = {}

    def fake_launch(buffer_dir: Path) -> dict[str, Any]:
        calls["buffer_dir"] = buffer_dir
        return {"launched": True, "pid": 123}

    monkeypatch.setattr(sidecar_mod, "launch_sidecar", fake_launch)
    res = client.post("/api/sidecar/start")
    assert res.status_code == 200
    body = res.json()
    assert body["launched"] is True and body["pid"] == 123
    assert "state" in body  # current state echoed back for the UI
    assert calls["buffer_dir"] is not None


def test_start_surfaces_launch_error_as_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(buffer_dir: Path) -> dict[str, Any]:
        raise sidecar_mod.SidecarLaunchError("run `npm install` in the sidecar/ directory")

    monkeypatch.setattr(sidecar_mod, "launch_sidecar", boom)
    res = client.post("/api/sidecar/start")
    assert res.status_code == 503
    assert "npm install" in res.json()["detail"]


def test_qr_404_when_absent_then_served(client: TestClient, tmp_path: Path) -> None:
    assert client.get("/api/sidecar/qr").status_code == 404

    (tmp_path / "qr.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    res = client.get("/api/sidecar/qr")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    assert res.headers.get("cache-control") == "no-store"
