"""Execution router (/api/execution): request validation + argv composition.

These tests stub the spawn layer (``app.webapp.runs``) so they assert the
router's contract — which CLI argv each action composes, and the 400/403/409
gates — without launching a subprocess. The real spawn → poll → funnel path is
covered end-to-end in ``test_execution_runs.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.webapp import runs
from app.webapp.server import create_app
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """A loopback client whose spawn layer is stubbed; records the argv composed."""
    box: dict[str, Any] = {}

    def fake_start(kind: str, argv: list[str], *, env_overrides: dict[str, str] | None = None):
        box["kind"] = kind
        box["argv"] = argv
        box["env"] = env_overrides
        return {"kind": kind, "run_id": "20260101T000000"}

    monkeypatch.setattr(runs, "start_run", fake_start)
    monkeypatch.setattr(runs, "active_run", lambda: None)
    monkeypatch.setattr(runs, "list_runs", lambda limit=50: [])

    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    with TestClient(app, client=LOOPBACK) as client:
        box["client"] = client
        yield box


def _run(box: dict[str, Any], body: dict[str, Any]):
    return box["client"].post("/api/execution/run", json=body)


def test_scan_live_argv(captured: dict[str, Any]) -> None:
    res = _run(captured, {"action": "scan", "mode": "live"})
    assert res.status_code == 200
    assert captured["argv"] == ["scan"]
    assert captured["env"] and "WR_DB_PATH" in captured["env"]


def test_scan_dry_run_new_only_argv(captured: dict[str, Any]) -> None:
    _run(captured, {"action": "scan", "mode": "dry_run"})
    assert captured["argv"] == ["scan", "--dry-run"]


def test_scan_dry_run_days_argv(captured: dict[str, Any]) -> None:
    _run(captured, {"action": "scan", "mode": "dry_run", "days": 7})
    assert captured["argv"] == ["scan", "--dry-run", "--days", "7"]


def test_process_and_notify_and_resync_argv(captured: dict[str, Any]) -> None:
    _run(captured, {"action": "process", "mode": "dry_run"})
    assert captured["argv"] == ["review", "--dry-run"]
    _run(captured, {"action": "notify", "run": 5})
    assert captured["argv"] == ["notify", "--run", "5"]
    _run(captured, {"action": "resync"})
    assert captured["argv"] == ["resync"]


def test_reprocess_requires_confirm(captured: dict[str, Any]) -> None:
    blocked = _run(captured, {"action": "reprocess"})
    assert blocked.status_code == 403
    ok = _run(captured, {"action": "reprocess", "confirm": True})
    assert ok.status_code == 200
    assert captured["argv"] == ["reprocess", "--confirm"]


def test_bad_inputs_rejected(captured: dict[str, Any]) -> None:
    assert _run(captured, {}).status_code == 400  # missing action
    assert _run(captured, {"action": "bogus"}).status_code == 400
    assert _run(captured, {"action": "scan", "mode": "weird"}).status_code == 400
    assert _run(captured, {"action": "scan", "mode": "dry_run", "days": 0}).status_code == 400
    assert _run(captured, {"action": "notify", "run": -1}).status_code == 400


def test_busy_returns_409(captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def busy_start(*a: Any, **k: Any):
        raise runs.RunBusyError("a scan run (X) is still in progress")

    monkeypatch.setattr(runs, "start_run", busy_start)
    res = _run(captured, {"action": "resync"})
    assert res.status_code == 409


def test_kill_unknown_run_404(captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs, "get_run", lambda *a, **k: None)
    res = captured["client"].post("/api/execution/runs/scan/nope/kill")
    assert res.status_code == 404


def test_syncs_endpoint_lists_recent_and_totals(tmp_path: Any) -> None:
    from app.webapp.server import create_app
    from src.db import store

    db = tmp_path / "syncs.sqlite3"
    conn = store.connect(db)
    store.record_sync(conn, source="resync", chats_added=2, chats_updated=1, messages_added=5)
    conn.close()

    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    with TestClient(app, client=LOOPBACK) as client:
        body = client.get("/api/execution/syncs").json()

    assert body["syncs"][0]["source"] == "resync"
    assert body["syncs"][0]["messages_added"] == 5
    assert body["syncs"][0]["chats_added"] == 2
    assert set(body["totals"]) == {"chats", "messages"}


def test_health_reports_connector_status(captured: dict[str, Any]) -> None:
    res = captured["client"].get("/api/execution/health")
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"name", "connected", "detail"}
    assert isinstance(body["connected"], bool)
