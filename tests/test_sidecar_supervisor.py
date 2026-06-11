"""Sidecar supervisor (src/connector/sidecar.py): state derivation + safe launch.

Fully offline and process-free: ``status.json`` is written by hand to drive every
lifecycle state, and the process spawner is injected so no Node is ever started.
The single-instance rule (never launch over a live sidecar, never kill) and the
relaunch-then-poll loop are asserted with a fake clock so there is no real wait.
"""

from __future__ import annotations

import json
import threading
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
    assert info.chats == 3 and info.messages == 7  # session counters still parsed
    assert "buffered" not in info.detail  # not presented as a misleading buffer total


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


# --- supervise_once: the keep-alive decision (#73) -------------------------

def test_supervise_once_noop_when_live(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buf"
    _write_status(buffer_dir, paired=True, connected=True, last_update=_fresh())

    def spawner(*a: Any, **k: Any) -> _FakeProc:
        raise AssertionError("must not relaunch a live sidecar")

    res = sidecar.supervise_once(
        buffer_dir, sidecar_root=_sidecar_root(tmp_path, with_deps=True), spawner=spawner
    )
    assert res["action"] == sidecar.ACTION_LIVE


def test_supervise_once_relaunches_when_stale(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buf"
    _write_status(buffer_dir, paired=True, connected=True, last_update=_old())
    called = False

    def spawner(*a: Any, **k: Any) -> _FakeProc:
        nonlocal called
        called = True
        return _FakeProc()

    res = sidecar.supervise_once(
        buffer_dir, sidecar_root=_sidecar_root(tmp_path, with_deps=True), spawner=spawner
    )
    assert res["action"] == sidecar.ACTION_RELAUNCHED
    assert called is True


def test_supervise_once_relaunches_when_stopped(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buf"  # no status.json at all → stopped
    called = False

    def spawner(*a: Any, **k: Any) -> _FakeProc:
        nonlocal called
        called = True
        return _FakeProc()

    res = sidecar.supervise_once(
        buffer_dir, sidecar_root=_sidecar_root(tmp_path, with_deps=True), spawner=spawner
    )
    assert res["action"] == sidecar.ACTION_RELAUNCHED
    assert called is True


def test_supervise_once_refuses_to_spawn_when_needs_qr(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buf"
    _write_status(buffer_dir, paired=False, connected=False, last_update=_fresh())

    def spawner(*a: Any, **k: Any) -> _FakeProc:
        raise AssertionError("relaunching cannot help an unpaired session")

    res = sidecar.supervise_once(
        buffer_dir, sidecar_root=_sidecar_root(tmp_path, with_deps=True), spawner=spawner
    )
    assert res["action"] == sidecar.ACTION_NEEDS_QR


def test_supervise_once_leaves_a_linking_session_alone(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buf"
    _write_status(buffer_dir, paired=True, connected=False, last_update=_fresh())

    def spawner(*a: Any, **k: Any) -> _FakeProc:
        raise AssertionError("a fresh, linking session is making progress")

    res = sidecar.supervise_once(
        buffer_dir, sidecar_root=_sidecar_root(tmp_path, with_deps=True), spawner=spawner
    )
    assert res["action"] == sidecar.ACTION_LINKING


def test_supervise_once_reports_launch_failure_without_raising(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buf"  # stopped
    # Deps missing → launch_sidecar raises SidecarLaunchError, which the supervisor
    # swallows into the detail so the loop survives to retry.
    res = sidecar.supervise_once(
        buffer_dir,
        sidecar_root=_sidecar_root(tmp_path, with_deps=False),
        spawner=lambda *a, **k: _FakeProc(),
    )
    assert res["action"] == sidecar.ACTION_RELAUNCHED
    assert "launch failed" in res["detail"]


# --- run_supervisor: the loop ----------------------------------------------

def test_run_supervisor_ticks_then_stops(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buf"
    _write_status(buffer_dir, paired=True, connected=True, last_update=_fresh())
    stop = threading.Event()
    ticks: list[str] = []

    def on_tick(result: dict[str, Any]) -> None:
        ticks.append(str(result["action"]))
        if len(ticks) >= 3:
            stop.set()  # end the loop after three checks

    # Each wait is a no-op that returns False (not stopped) so the loop proceeds;
    # the stop is driven by on_tick instead, so there is no real sleeping.
    sidecar.run_supervisor(
        buffer_dir, stop, interval=0.0,
        sidecar_root=_sidecar_root(tmp_path, with_deps=True),
        spawner=lambda *a, **k: _FakeProc(), on_tick=on_tick,
    )
    assert ticks == [sidecar.ACTION_LIVE, sidecar.ACTION_LIVE, sidecar.ACTION_LIVE]


def test_run_supervisor_exits_immediately_when_already_stopped(tmp_path: Path) -> None:
    buffer_dir = tmp_path / "buf"
    stop = threading.Event()
    stop.set()
    calls = 0

    def on_tick(_result: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1

    sidecar.run_supervisor(buffer_dir, stop, interval=0.0, on_tick=on_tick)
    assert calls == 0  # a pre-set stop means no tick runs


# --- wait_for_settled: the buffer quiescence gate (#73) --------------------

def _append(buffer_dir: Path, name: str, line: str) -> None:
    buffer_dir.mkdir(parents=True, exist_ok=True)
    with (buffer_dir / name).open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def test_wait_for_settled_disabled_returns_immediately(tmp_path: Path) -> None:
    def clock() -> float:
        raise AssertionError("must not consult the clock when disabled")

    assert sidecar.wait_for_settled(
        tmp_path, settle_seconds=0, timeout_seconds=90, clock=clock
    ) is True


def test_wait_for_settled_returns_true_once_quiet(tmp_path: Path) -> None:
    # The buffer grows on the first two polls, then goes quiet long enough to settle.
    _append(tmp_path, "messages.ndjson", "a")
    times = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0])

    grew = {"polls": 0}

    def sleep(_s: float) -> None:
        grew["polls"] += 1
        if grew["polls"] <= 2:
            _append(tmp_path, "messages.ndjson", "more")

    settled = sidecar.wait_for_settled(
        tmp_path, settle_seconds=2.0, timeout_seconds=100.0,
        poll_interval=1.0, sleep=sleep, clock=lambda: next(times),
    )
    assert settled is True


def test_wait_for_settled_times_out_while_growing(tmp_path: Path) -> None:
    # The buffer never stops growing → the hard cap fires and we proceed anyway.
    times = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])

    def sleep(_s: float) -> None:
        _append(tmp_path, "messages.ndjson", "x")

    settled = sidecar.wait_for_settled(
        tmp_path, settle_seconds=2.0, timeout_seconds=3.0,
        poll_interval=1.0, sleep=sleep, clock=lambda: next(times),
    )
    assert settled is False
