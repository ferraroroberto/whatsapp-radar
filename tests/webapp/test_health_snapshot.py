"""Source health reads its slow probes from the demand-driven snapshot.

``/api/execution/health`` used to probe every connector (Gmail token refresh +
profile round trips) on each request, so a poll cost the upstream's latency.
The probes now come from memory; the database-derived fields stay live.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.webapp.routers import execution as execution_router
from app.webapp.server import create_app
from src.config import Config, HubConfig, TelegramConfig
from src.connector.base import ConnectorStatus
from src.connector.factory import ConnectorBinding
from src.db import store
from src.demand_snapshot import DemandSnapshot
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)


class CountingConnector:
    """Connector whose ``connect`` is the slow upstream call; counts how often it runs."""

    def __init__(self) -> None:
        self.connects = 0

    def connect(self) -> ConnectorStatus:
        self.connects += 1
        return ConnectorStatus("counting-fake", True, f"probe {self.connects}")

    def status(self) -> ConnectorStatus:
        return ConnectorStatus("counting-fake", False, "unknown")

    def stop(self) -> None:
        return None


def _config(tmp_path: Path, **overrides: Any) -> Config:
    return Config(
        db_path=tmp_path / "health.sqlite3",
        connector="fixture",
        classifier="stub",
        hub=HubConfig("http://127.0.0.1:8000", "stub"),
        notifier="none",
        telegram=TelegramConfig("", ""),
        linked_device_dir=tmp_path / "buffer",
        **overrides,
    )


@pytest.fixture
def probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    state: dict[str, Any] = {"cfg": _config(tmp_path), "connector": CountingConnector()}
    monkeypatch.setattr(execution_router, "load_config", lambda: state["cfg"])
    monkeypatch.setattr(
        execution_router,
        "build_connectors",
        lambda _cfg: [ConnectorBinding("whatsapp", state["connector"])],  # type: ignore[arg-type]
    )
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = state["cfg"].db_path
    with TestClient(app, client=LOOPBACK) as client:
        state["client"] = client
        yield state


def test_repeat_reads_do_not_reprobe_the_upstream(probe: dict[str, Any]) -> None:
    client: TestClient = probe["client"]
    first = client.get("/api/execution/health").json()
    second = client.get("/api/execution/health").json()
    assert probe["connector"].connects == 1
    assert second["sources"][0]["detail"] == first["sources"][0]["detail"]
    assert second["probed_age_s"] >= first["probed_age_s"] >= 0


def test_a_config_change_rebuilds_instead_of_serving_the_old_probe(
    probe: dict[str, Any], tmp_path: Path
) -> None:
    client: TestClient = probe["client"]
    client.get("/api/execution/health")
    probe["cfg"] = _config(tmp_path, sources=("whatsapp", "gmail"))
    body = client.get("/api/execution/health").json()
    assert probe["connector"].connects == 2
    assert body["sources"][0]["detail"] == "probe 2"


def test_database_derived_fields_stay_live_between_probes(probe: dict[str, Any]) -> None:
    client: TestClient = probe["client"]
    before = client.get("/api/execution/health").json()["sources"][0]
    conn = store.connect(probe["cfg"].db_path)
    try:
        store.record_sync(conn, source="resync", chats_added=1, chats_updated=0, messages_added=3)
    finally:
        conn.close()
    after = client.get("/api/execution/health").json()["sources"][0]
    assert probe["connector"].connects == 1
    assert before["last_attempt"] is None or before["last_attempt"] != after["last_attempt"]


class _Clock:
    """A build counter that can be told to fail."""

    def __init__(self) -> None:
        self.builds = 0
        self.fail = False

    def build(self) -> int:
        self.builds += 1
        if self.fail:
            raise RuntimeError("upstream down")
        return self.builds


def _snapshot(clock: _Clock, **kw: float) -> DemandSnapshot[int]:
    opts = {"refresh_s": 60.0, "demand_s": 180.0, "stale_s": 300.0, **kw}
    return DemandSnapshot(clock.build, lambda: "k", **opts)


def test_snapshot_serves_memory_until_stale_then_rebuilds_inline() -> None:
    clock = _Clock()
    snap = _snapshot(clock, stale_s=0.05)
    try:
        assert snap.get()[0] == 1
        assert snap.get()[0] == 1
        time.sleep(0.1)
        value, age = snap.get()
        assert (value, age) == (2, 0.0)
    finally:
        snap.close()


def test_snapshot_does_not_cache_a_failed_build() -> None:
    clock = _Clock()
    snap = _snapshot(clock)
    try:
        clock.fail = True
        with pytest.raises(RuntimeError):
            snap.get()
        clock.fail = False
        assert snap.get()[0] == 2
    finally:
        snap.close()


def test_snapshot_refreshes_in_the_background_only_while_read() -> None:
    clock = _Clock()
    snap = _snapshot(clock, refresh_s=0.05, demand_s=0.3)
    try:
        snap.get()
        deadline = time.monotonic() + 3
        while clock.builds < 3 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert clock.builds >= 3, "no background refresh while the snapshot was being read"
        time.sleep(0.5)  # demand window elapses; the ticker must stop
        settled = clock.builds
        time.sleep(0.3)
        assert clock.builds == settled
        # The next read starts a fresh ticker.
        snap.get()
        deadline = time.monotonic() + 3
        while clock.builds <= settled + 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert clock.builds > settled + 1
    finally:
        snap.close()


def test_concurrent_cold_readers_share_one_build() -> None:
    clock = _Clock()
    started = threading.Barrier(4)

    def slow_build() -> int:
        time.sleep(0.2)
        return clock.build()

    snap: DemandSnapshot[int] = DemandSnapshot(
        slow_build, lambda: "k", refresh_s=60, demand_s=180, stale_s=300
    )
    results: list[int] = []

    def read() -> None:
        started.wait()
        results.append(snap.get()[0])

    threads = [threading.Thread(target=read) for _ in range(4)]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
    finally:
        snap.close()
    assert clock.builds == 1
    assert results == [1, 1, 1, 1]
