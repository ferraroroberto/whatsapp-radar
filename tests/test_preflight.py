"""Connector preflight gate (src/connector/preflight.py).

Asserts the hard liveness check and the linked-device self-heal: a connected
source passes; an offline one raises :class:`ConnectorOffline`; and for the
linked-device connector a relaunch is attempted once (stubbed — no real sidecar)
before giving up. ``connect()`` is scripted per-call so the "offline → relaunch →
back online" sequence is deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Config, HubConfig, TelegramConfig
from src.connector import preflight as preflight_mod
from src.connector.base import ConnectorStatus
from src.connector.preflight import ConnectorOffline, ensure_connected, preflight


class _ScriptedConnector:
    """Returns a queued status on each ``connect()`` (last one repeats)."""

    def __init__(self, statuses: list[ConnectorStatus]) -> None:
        self._statuses = statuses
        self.connects = 0

    def connect(self) -> ConnectorStatus:
        i = min(self.connects, len(self._statuses) - 1)
        self.connects += 1
        return self._statuses[i]

    def status(self) -> ConnectorStatus:
        return self._statuses[min(self.connects, len(self._statuses) - 1)]


def _online() -> ConnectorStatus:
    return ConnectorStatus(name="linked_device", connected=True, detail="ok")


def _offline() -> ConnectorStatus:
    return ConnectorStatus(name="linked_device", connected=False, detail="heartbeat stale")


def _config(connector: str = "linked_device", *, autostart: bool = True) -> Config:
    return Config(
        db_path=Path("unused.sqlite3"),
        connector=connector,
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="m"),
        notifier="none",
        telegram=TelegramConfig(bot_token="", chat_id=""),
        linked_device_dir=Path("ld"),
        sidecar_autostart=autostart,
    )


def test_ensure_connected_passes_and_raises() -> None:
    assert ensure_connected(_ScriptedConnector([_online()])).connected is True
    with pytest.raises(ConnectorOffline):
        ensure_connected(_ScriptedConnector([_offline()]))


def test_preflight_returns_when_already_connected() -> None:
    conn = _ScriptedConnector([_online()])
    assert preflight(_config(), conn).connected is True
    assert conn.connects == 1  # no relaunch needed


def test_preflight_no_relaunch_for_fixture() -> None:
    # A non-linked-device connector never triggers the sidecar self-heal.
    with pytest.raises(ConnectorOffline):
        preflight(_config(connector="fixture"), _ScriptedConnector([_offline()]))


def test_preflight_respects_autostart_off(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def spy(*a: object, **k: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("should not be reached")

    monkeypatch.setattr(preflight_mod, "ensure_running", spy)
    with pytest.raises(ConnectorOffline):
        preflight(_config(autostart=False), _ScriptedConnector([_offline()]))
    assert called is False


def test_preflight_relaunches_then_recovers(monkeypatch: pytest.MonkeyPatch) -> None:
    relaunched: dict[str, object] = {}

    def fake_ensure_running(buffer_dir: Path, **_kw: object) -> object:
        relaunched["dir"] = buffer_dir
        return type("S", (), {"state": "running", "detail": "up"})()

    monkeypatch.setattr(preflight_mod, "ensure_running", fake_ensure_running)
    # offline on the first check, online after the relaunch.
    conn = _ScriptedConnector([_offline(), _online()])

    status = preflight(_config(), conn)
    assert status.connected is True
    assert relaunched["dir"] == Path("ld")


def test_preflight_raises_when_relaunch_does_not_help(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_ensure_running(buffer_dir: Path, **_kw: object) -> object:
        return type("S", (), {"state": "needs_qr", "detail": "scan the QR"})()

    monkeypatch.setattr(preflight_mod, "ensure_running", fake_ensure_running)
    with pytest.raises(ConnectorOffline):
        preflight(_config(), _ScriptedConnector([_offline(), _offline()]))
