"""Sidecar lifecycle: read its liveness state and (re)launch it when down.

The Node/Baileys sidecar (``sidecar/index.js``) is the only process that speaks
the WhatsApp protocol; the Python side is a read-only reader of the NDJSON buffer
it writes. This module is the thin supervisor that the rest of the system needs
but did not have: it derives a *coarse lifecycle state* from the sidecar's
heartbeat file and can spawn the process when it has stopped — without ever
killing a live one.

It stays firmly on the read-only side of the connector boundary: launching the
Node process is process management, not a WhatsApp write. Nothing here sends,
reacts, or reads receipts.

Liveness is the heartbeat: the sidecar rewrites ``status.json`` every 30s (and on
every event), so a file fresher than :data:`STALE_AFTER_SECONDS` means a live,
paired session. The coarse states the UI and the preflight gate care about:

- ``running``   — paired, connected, heartbeat fresh. The happy path.
- ``connecting``— paired and fresh but not yet ``connected`` (just launched / linking).
- ``stale``     — paired but the heartbeat is old: the process likely died and is
  safe to relaunch (auth is still valid, so no QR is needed).
- ``needs_qr``  — not paired (first run or logged out): a human must scan the QR.
- ``stopped``   — no ``status.json`` at all: never started here.

Single-instance discipline (the fleet's "never blanket-kill a live holder" rule):
:func:`launch_sidecar` refuses to spawn when a live sidecar is already detected,
and never terminates anything. Recovery is always *wait/relaunch*, never *kill*.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from src.connector.linked_device import is_heartbeat_fresh, read_status_file

# States, ordered roughly worst → best for the UI to colour.
STATE_STOPPED = "stopped"
STATE_NEEDS_QR = "needs_qr"
STATE_STALE = "stale"
STATE_CONNECTING = "connecting"
STATE_RUNNING = "running"

# A process spawner with Popen's shape; injected in tests so nothing is launched.
Spawner = Callable[..., "subprocess.Popen[bytes]"]


def sidecar_dir() -> Path:
    """The ``sidecar/`` directory holding ``index.js`` and ``node_modules``."""
    return Path(__file__).resolve().parents[2] / "sidecar"


class SidecarLaunchError(RuntimeError):
    """Raised when the sidecar process cannot be launched (e.g. deps missing)."""


@dataclass(frozen=True)
class SidecarStateInfo:
    """A coarse, UI-ready snapshot of the sidecar derived from its heartbeat."""

    state: str
    detail: str
    paired: bool
    connected: bool
    fresh: bool
    has_qr: bool
    chats: int
    messages: int
    last_update: str | None

    @property
    def is_live(self) -> bool:
        """True only when a paired session is connected with a fresh heartbeat."""
        return self.state == STATE_RUNNING

    @property
    def is_relaunchable(self) -> bool:
        """True when relaunching the process could recover it without a new QR."""
        return self.state in (STATE_STOPPED, STATE_STALE, STATE_CONNECTING)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["is_live"] = self.is_live
        data["is_relaunchable"] = self.is_relaunchable
        return data


def _status_path(buffer_dir: Path) -> Path:
    return buffer_dir / "status.json"


def _qr_path(buffer_dir: Path) -> Path:
    return buffer_dir / "qr.png"


def sidecar_state(buffer_dir: Path) -> SidecarStateInfo:
    """Derive the coarse lifecycle state from the sidecar's heartbeat file.

    Reuses :func:`read_status_file` and :func:`is_heartbeat_fresh` from the
    linked-device reader so there is exactly one definition of "stale" across the
    codebase.
    """
    raw = read_status_file(buffer_dir)
    has_qr = _qr_path(buffer_dir).is_file()
    if raw is None:
        return SidecarStateInfo(
            state=STATE_STOPPED,
            detail="sidecar not started — launch it to connect",
            paired=False,
            connected=False,
            fresh=False,
            has_qr=has_qr,
            chats=0,
            messages=0,
            last_update=None,
        )

    paired = bool(raw.get("paired"))
    connected = bool(raw.get("connected"))
    last_update = raw.get("last_update")
    fresh = is_heartbeat_fresh(last_update)
    chats = int(raw.get("chats", 0) or 0)
    messages = int(raw.get("messages", 0) or 0)
    last = last_update if isinstance(last_update, str) else None

    if not paired:
        state, detail = STATE_NEEDS_QR, "not paired — scan the QR to link a device"
    elif not fresh:
        state, detail = STATE_STALE, "heartbeat stale — the sidecar process may have stopped"
    elif not connected:
        state, detail = STATE_CONNECTING, "linking — waiting for WhatsApp to connect"
    else:
        # The sidecar's own counters are per-session (they reset on reconnect), so
        # don't present them as buffer totals — the UI shows the truthful *stored*
        # totals + last-sync delta instead (#31).
        state, detail = STATE_RUNNING, "connected — receiving live updates"

    return SidecarStateInfo(
        state=state,
        detail=detail,
        paired=paired,
        connected=connected,
        fresh=fresh,
        has_qr=has_qr,
        chats=chats,
        messages=messages,
        last_update=last,
    )


def _pid_path(buffer_dir: Path) -> Path:
    return buffer_dir / "sidecar.pid"


def _process_alive(pid: int) -> bool:
    """Best-effort check that a PID is a live process (cross-platform, never raises)."""
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes  # local import: Windows-only

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except (OSError, ValueError, AttributeError):
        return False


def _tracked_pid_alive(buffer_dir: Path) -> bool:
    """True if the PID file we wrote points at a still-running process."""
    pid_file = _pid_path(buffer_dir)
    if not pid_file.is_file():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return _process_alive(pid)


def is_running(buffer_dir: Path) -> bool:
    """True if a sidecar is live: a fresh heartbeat or a tracked, alive PID.

    The heartbeat is the primary signal (it tells us the process is *actually*
    talking to WhatsApp); the PID covers the brief window right after launch
    before the first heartbeat lands.
    """
    if sidecar_state(buffer_dir).fresh:
        return True
    return _tracked_pid_alive(buffer_dir)


def launch_sidecar(
    buffer_dir: Path,
    *,
    sidecar_root: Path | None = None,
    node_bin: str | None = None,
    spawner: Spawner | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Spawn the Node sidecar detached, unless one is already live.

    Never kills anything: if a live sidecar is detected the call is a no-op
    (``{"launched": False, "reason": "already running"}``). The child's combined
    output is redirected to ``<buffer_dir>/sidecar.log`` so the QR and any errors
    are inspectable headlessly, and its PID is recorded for liveness checks.

    Raises :class:`SidecarLaunchError` if the sidecar dependencies are not
    installed (``npm install`` was never run) — a clear, actionable failure
    rather than a crash-loop.
    """
    if is_running(buffer_dir):
        return {"launched": False, "reason": "already running"}

    root = sidecar_root or sidecar_dir()
    if not (root / "index.js").is_file():
        raise SidecarLaunchError(f"sidecar entry point not found at {root / 'index.js'}")
    if not (root / "node_modules").is_dir():
        raise SidecarLaunchError(
            "sidecar dependencies are not installed — run `npm install` in the sidecar/ directory"
        )

    node = node_bin or os.environ.get("WR_NODE_BIN", "node")
    child_env = {**os.environ, **(env or {}), "WR_LINKED_DEVICE_DIR": str(buffer_dir)}
    buffer_dir.mkdir(parents=True, exist_ok=True)
    spawn = spawner or _default_spawner

    log_fh = (buffer_dir / "sidecar.log").open("ab")
    try:
        proc = spawn(
            [node, "index.js"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            env=child_env,
            creationflags=_creationflags(),
            close_fds=True,
        )
    except OSError as exc:
        raise SidecarLaunchError(
            f"could not start node ({node!r}) — is Node.js installed and on PATH? {exc}"
        ) from exc
    finally:
        # The detached child inherited its own handle at spawn time; the parent's
        # copy is no longer needed and must be released (avoids a leaked handle).
        log_fh.close()

    _pid_path(buffer_dir).write_text(str(proc.pid), encoding="utf-8")
    return {"launched": True, "pid": proc.pid}


def _default_spawner(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
    return subprocess.Popen(*args, **kwargs)


def _creationflags() -> int:
    flags = 0
    for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS"):
        flags |= getattr(subprocess, name, 0)
    return flags


def ensure_running(
    buffer_dir: Path,
    *,
    sidecar_root: Path | None = None,
    node_bin: str | None = None,
    spawner: Spawner | None = None,
    wait_seconds: float = 25.0,
    poll_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> SidecarStateInfo:
    """Bring the sidecar to ``running`` if possible, returning the final state.

    If it is already live, returns immediately. Otherwise it launches the process
    (a relaunch when stale/stopped, or to (re)emit a QR when unpaired) and polls
    the heartbeat up to ``wait_seconds`` for the session to come up. A session
    that needs a fresh QR will never reach ``running`` here — the call returns the
    ``needs_qr`` state so the caller can surface the QR to the operator.

    ``sleep`` / ``clock`` are injectable so tests drive it without real waiting.
    """
    info = sidecar_state(buffer_dir)
    if info.is_live:
        return info

    launch_sidecar(
        buffer_dir, sidecar_root=sidecar_root, node_bin=node_bin, spawner=spawner
    )

    deadline = clock() + max(0.0, wait_seconds)
    while clock() < deadline:
        info = sidecar_state(buffer_dir)
        if info.is_live:
            return info
        sleep(poll_interval)
    return sidecar_state(buffer_dir)


# --- continuous supervision (the tray keep-alive, #73) ---------------------

# How often the tray's keep-alive thread re-checks the sidecar. Frequent and
# cheap: a fresh buffer means a scan reads an already-warm source rather than a
# cold backfill. Overridable so a box can tune it without a code change.
SUPERVISE_INTERVAL_SECONDS = float(os.environ.get("WR_SIDECAR_SUPERVISE_SECONDS", "90") or "90")

# Supervisor tick outcomes (what one check decided).
ACTION_LIVE = "live"  # already connected — nothing to do
ACTION_RELAUNCHED = "relaunched"  # process was gone/stale — spawned a fresh one
ACTION_NEEDS_QR = "needs_qr"  # unpaired — a human must scan the QR; do NOT relaunch
ACTION_LINKING = "linking"  # a process is up and linking — leave it alone


def supervise_once(
    buffer_dir: Path,
    *,
    sidecar_root: Path | None = None,
    node_bin: str | None = None,
    spawner: Spawner | None = None,
) -> dict[str, Any]:
    """Run one keep-alive check, relaunching the sidecar only when truly gone.

    The decision, by coarse state:

    - ``running``    → :data:`ACTION_LIVE`: connected, nothing to do.
    - ``connecting`` → :data:`ACTION_LINKING`: a process is up and linking; leave
      it (relaunching would interrupt a session that is making progress).
    - ``stale`` / ``stopped`` → :data:`ACTION_RELAUNCHED`: the process is gone, so
      relaunch it. ``launch_sidecar`` is itself a no-op over a live holder, so this
      can never kill or double-spawn.
    - ``needs_qr``   → :data:`ACTION_NEEDS_QR`: unpaired; relaunching cannot help
      (a human must scan the QR), so we never spawn and let the caller surface it.

    Returns ``{"action": <ACTION_*>, "state": <coarse state>, "detail": <str>}``.
    Never raises on a failed launch — a transient spawn error is reported in the
    ``detail`` so the loop keeps trying on the next tick rather than dying.
    """
    info = sidecar_state(buffer_dir)
    if info.is_live:
        return {"action": ACTION_LIVE, "state": info.state, "detail": info.detail}
    if info.state == STATE_NEEDS_QR:
        return {"action": ACTION_NEEDS_QR, "state": info.state, "detail": info.detail}
    if info.state == STATE_CONNECTING:
        return {"action": ACTION_LINKING, "state": info.state, "detail": info.detail}

    # stopped or stale: the process is gone — relaunch (never kills a live holder).
    try:
        launch_sidecar(
            buffer_dir, sidecar_root=sidecar_root, node_bin=node_bin, spawner=spawner
        )
        return {"action": ACTION_RELAUNCHED, "state": info.state, "detail": info.detail}
    except SidecarLaunchError as exc:
        return {"action": ACTION_RELAUNCHED, "state": info.state, "detail": f"launch failed: {exc}"}


def run_supervisor(
    buffer_dir: Path,
    stop_event: threading.Event,
    *,
    interval: float = SUPERVISE_INTERVAL_SECONDS,
    sidecar_root: Path | None = None,
    node_bin: str | None = None,
    spawner: Spawner | None = None,
    on_tick: Callable[[dict[str, Any]], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Loop :func:`supervise_once` every ``interval`` seconds until ``stop_event``.

    Checks immediately on entry, then waits ``interval`` (interruptibly, via
    ``stop_event.wait``) between ticks so a quit returns promptly. ``on_tick``
    receives each tick's result dict — the tray uses it to surface a one-time
    ``needs_qr`` toast. ``sleep`` is unused by default (the wait is the event's)
    but kept injectable for tests that prefer to drive it directly.

    Designed to run in a daemon thread; it owns no resources and never raises.
    """
    while not stop_event.is_set():
        result = supervise_once(
            buffer_dir, sidecar_root=sidecar_root, node_bin=node_bin, spawner=spawner
        )
        if on_tick is not None:
            on_tick(result)
        if stop_event.wait(interval):
            break


# --- settled-buffer gate (don't read a buffer that is still filling, #73) ---


def _buffer_signature(buffer_dir: Path) -> tuple[int, int]:
    """Append-only size signature of the NDJSON buffer — a quiescence signal.

    Sizes of ``messages.ndjson`` + ``chats.ndjson`` only ever grow as the sidecar
    appends, so an unchanged signature means no new writes landed. Sizes are
    robust to the sidecar's per-session counters resetting on reconnect (unlike
    the ``status.json`` counts). A missing file reads as 0.
    """
    sizes = []
    for name in ("messages.ndjson", "chats.ndjson"):
        try:
            sizes.append((buffer_dir / name).stat().st_size)
        except OSError:
            sizes.append(0)
    return (sizes[0], sizes[1])


def wait_for_settled(
    buffer_dir: Path,
    *,
    settle_seconds: float,
    timeout_seconds: float,
    poll_interval: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Block until the buffer stops growing, so a read doesn't miss a live backfill.

    After a (re)connect Baileys streams history into the buffer asynchronously;
    reading mid-stream under-reports. This polls the append-only buffer signature
    and returns ``True`` once it has been unchanged for ``settle_seconds``. It is
    hard-capped at ``timeout_seconds`` (returns ``False`` — the caller proceeds
    anyway, bounded, so a chatty live group can't stall a run forever).

    A non-positive ``settle_seconds`` disables the gate (returns ``True`` at once),
    which is how the fixture connector and an opted-out box skip it. ``sleep`` /
    ``clock`` are injectable so tests drive it without real waiting.
    """
    if settle_seconds <= 0:
        return True

    start = clock()
    deadline = start + max(0.0, timeout_seconds)
    last_sig = _buffer_signature(buffer_dir)
    quiet_since = start
    while clock() < deadline:
        sleep(poll_interval)
        sig = _buffer_signature(buffer_dir)
        now = clock()
        if sig != last_sig:
            last_sig = sig
            quiet_since = now
        elif now - quiet_since >= settle_seconds:
            return True
    return False
