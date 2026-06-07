"""Sidecar supervisor (src/connector/sidecar.py): state derivation + safe launch.

Fully offline and process-free: ``status.json`` is written by hand to drive every
lifecycle state, and the process spawner is injected so no Node is ever started.
The single-instance rule (never launch over a live sidecar, never kill) and the
relaunch-then-poll loop are asserted with a fake clock so there is no real wait.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.connector import sidecar


def _write_status(buffer_dir: Path, **fields: Any) -> None:
    buffer_dir.mkdir(parents=True, exist_ok=True)
    (buffer_dir / "status.json").write_text(json.dumps(fields), encoding="utf-8")


def _fresh() -> str:
    return datetime.now(UTC).isoformat()


def _old() -> str:
    return (datetime.now(UTC) - timedelta(minutes=10)).isoformat()


class _FakeProc:
    def __init__(self, pid: int = 4321) -> None:
        self.pid = pid


def _sidecar_root(tmp_path: Path, *, with_deps: bool) -> Path:
    root = tmp_path / "sidecar"
    root.mkdir()
    (root / "index.js").write_text("// fake", encoding="utf-8")
    if with_deps:
        (root / "node_modules").mkdir()
    return root


# --- state derivation ------------------------------------------------------

def test_state_stopped_when_no_status(tmp_path: Path) -> None:
    info = sidecar.sidecar_state(tmp_path)
    assert info.state == sidecar.STATE_STOPPED
    assert not info.is_live and info.is_relaunchable


def test_state_needs_qr_when_unpaired(tmp_path: Path) -> None:
    _write_status(tmp_path, paired=False, connected=False, last_update=_fresh())
    (tmp_path / "qr.png").write_bytes(b"\x89PNG")
    info = sidecar.sidecar_state(tmp_path)
    assert info.state == sidecar.STATE_NEEDS_QR
    assert info.has_qr is True
    assert not info.is_relaunchable  # a QR scan is required, not a relaunch


def test_state_stale_when_heartbeat_old(tmp_path: Path) -> None:
    _write_status(tmp_path, paired=True, connected=True, last_update=_old(), chats=5, messages=9)
    info = sidecar.sidecar_state(tmp_path)
    assert info.state == sidecar.STATE_STALE
    assert info.is_relaunchable and not info.is_live


def test_state_connecting_when_fresh_but_not_connected(tmp_path: Path) -> None:
    _write_status(tmp_path, paired=True, connected=False, last_update=_fresh())
    assert sidecar.sidecar_state(tmp_path).state == sidecar.STATE_CONNECTING


def test_state_running_when_paired_connected_fresh(tmp_path: Path) -> None:
    _write_status(tmp_path, paired=True, connected=True, last_update=_fresh(), chats=3, messages=7)
    info = sidecar.sidecar_state(tmp_path)
    assert info.state == sidecar.STATE_RUNNING
    assert info.is_live
    assert "3 chats" in info.detail and "7 messages" in info.detail


# --- launch: single-instance + clear failures ------------------------------

def test_launch_is_noop_when_already_live(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buf"
    _write_status(buffer_dir, paired=True, connected=True, last_update=_fresh())
    called = False

    def spawner(*a: Any, **k: Any) -> _FakeProc:
        nonlocal called
        called = True
        return _FakeProc()

    res = sidecar.launch_sidecar(buffer_dir, sidecar_root=_sidecar_root(tmp_path, with_deps=True),
                                 spawner=spawner)
    assert res == {"launched": False, "reason": "already running"}
    assert called is False  # never spawned over a live sidecar


def test_launch_raises_when_deps_missing(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buf"
    with pytest.raises(sidecar.SidecarLaunchError, match="npm install"):
        sidecar.launch_sidecar(
            buffer_dir, sidecar_root=_sidecar_root(tmp_path, with_deps=False),
            spawner=lambda *a, **k: _FakeProc(),
        )


def test_launch_spawns_and_records_pid(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buf"
    captured: dict[str, Any] = {}

    def spawner(args: list[str], **kwargs: Any) -> _FakeProc:
        captured["args"] = args
        captured["cwd"] = kwargs.get("cwd")
        return _FakeProc(pid=9999)

    root = _sidecar_root(tmp_path, with_deps=True)
    res = sidecar.launch_sidecar(buffer_dir, sidecar_root=root, spawner=spawner)

    assert res == {"launched": True, "pid": 9999}
    assert captured["args"][1] == "index.js"
    assert captured["cwd"] == str(root)
    assert (buffer_dir / "sidecar.pid").read_text(encoding="utf-8") == "9999"


# --- ensure_running: relaunch then poll ------------------------------------

def test_ensure_running_short_circuits_when_live(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buf"
    _write_status(buffer_dir, paired=True, connected=True, last_update=_fresh())

    def spawner(*a: Any, **k: Any) -> _FakeProc:
        raise AssertionError("must not launch when already live")

    info = sidecar.ensure_running(buffer_dir, sidecar_root=_sidecar_root(tmp_path, with_deps=True),
                                  spawner=spawner)
    assert info.is_live


def test_ensure_running_launches_then_polls_to_live(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buf"
    root = _sidecar_root(tmp_path, with_deps=True)
    # Spawner simulates the sidecar coming up "connecting" the moment it starts.
    def spawner(*a: Any, **k: Any) -> _FakeProc:
        _write_status(buffer_dir, paired=True, connected=False, last_update=_fresh())
        return _FakeProc()

    # On the first poll-sleep, the session finishes linking and goes live.
    def fake_sleep(_seconds: float) -> None:
        _write_status(buffer_dir, paired=True, connected=True, last_update=_fresh())

    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    info = sidecar.ensure_running(
        buffer_dir, sidecar_root=root, spawner=spawner,
        wait_seconds=10.0, poll_interval=0.01, sleep=fake_sleep, clock=lambda: next(ticks),
    )
    assert info.is_live


def test_ensure_running_gives_up_when_qr_needed(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buf"
    root = _sidecar_root(tmp_path, with_deps=True)
    # A logged-out session: launching only ever produces an unpaired (needs_qr) state.
    def spawner(*a: Any, **k: Any) -> _FakeProc:
        _write_status(buffer_dir, paired=False, connected=False, last_update=_fresh())
        return _FakeProc()

    ticks = iter([0.0, 1.0, 2.0])
    info = sidecar.ensure_running(
        buffer_dir, sidecar_root=root, spawner=spawner,
        wait_seconds=1.0, poll_interval=0.01, sleep=lambda _s: None, clock=lambda: next(ticks),
    )
    assert info.state == sidecar.STATE_NEEDS_QR
    assert not info.is_live
