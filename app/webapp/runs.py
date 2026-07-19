"""Run-record + output-streaming infra for the Execution tab.

Mirrors App Launcher's job-run model (``src/jobs.py`` there): each Execution
action runs as a detached subprocess whose combined stdout+stderr streams to
``output.log``, beside a ``run.json`` holding lifecycle metadata plus the parsed
structured result. The webapp polls the run directory — a finished run is a
static log, a running one keeps growing — so the phone sees the process byte by
byte exactly as it would in App Launcher's Jobs tab.

Why a subprocess and not an in-process call: the Execution tab is where the
operator *validates* the very command App Launcher Jobs will schedule
(``python launcher.py scan|resync|reprocess``). Running the identical process
here means what you watch is byte-for-byte what runs there. The structured
outcome (funnel / counts) rides back on the ``__WR_RESULT__`` sentinel line the
CLI prints last (see :mod:`src.runresult`).

Single-flight: only one run at a time, because every action shares the one
SQLite store and connector buffer. A second request while one is in flight is
rejected (the router turns :class:`RunBusyError` into HTTP 409).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from app.webapp.routers._helpers import PROJECT_ROOT
from src.runresult import parse_result

RUNS_DIR = PROJECT_ROOT / "webapp" / "runs"
_LAUNCHER = PROJECT_ROOT / "launcher.py"
# Cap the tail we scan for the result sentinel + return to the UI. Generous —
# a scan's per-chat progress for a realistic monitored set stays well under it.
_TAIL_BYTES = 256 * 1024

# Single-flight guard. The webapp is one process, so an in-memory handle to the
# one active run is enough; on restart an interrupted run is simply orphaned
# (its on-disk status stays "running" until re-derived — acceptable, matching
# App Launcher's stuck-run handling).
_LOCK = threading.Lock()
_ACTIVE: dict[str, Any] | None = None


class RunBusyError(Exception):
    """Raised when a run is requested while another is still in flight."""


def _now_iso() -> str:
    # UTC with an explicit offset — the same discipline as the DB store, so the
    # same run can never show two different times across surfaces (#163).
    return datetime.now(UTC).isoformat(timespec="seconds")


def _python() -> str:
    """The interpreter to launch the CLI with — this repo's venv, else current."""
    candidate = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.is_file() else sys.executable


def _creationflags() -> int:
    flags = 0
    for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS"):
        flags |= getattr(subprocess, name, 0)
    return flags


# ----------------------------------------------------------- run records


def new_run_id() -> str:
    """A sortable, filesystem-safe run id (second resolution)."""
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _new_run_dir(kind: str, run_id: str) -> Path:
    """Create ``webapp/runs/<kind>/<run_id>/``, disambiguating same-second ids."""
    base = RUNS_DIR / kind
    base.mkdir(parents=True, exist_ok=True)
    target = base / run_id
    n = 2
    while target.exists():
        target = base / f"{run_id}-{n}"
        n += 1
    target.mkdir()
    return target


def write_run_json(run_dir: Path, **fields: Any) -> None:
    """Atomic, merging write of ``run_dir/run.json`` (skips ``None`` values)."""
    target = run_dir / "run.json"
    existing: dict[str, Any] = {}
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing.update({k: v for k, v in fields.items() if v is not None})
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    os.replace(tmp, target)


def read_run(run_dir: Path) -> dict[str, Any]:
    """Read ``run.json``; missing/corrupt → empty dict."""
    target = run_dir / "run.json"
    if not target.exists():
        return {}
    try:
        data: dict[str, Any] = json.loads(target.read_text(encoding="utf-8"))
        return data
    except (OSError, json.JSONDecodeError):
        return {}


def read_output_tail(run_dir: Path, max_bytes: int = _TAIL_BYTES) -> str:
    """Up to the last ``max_bytes`` of ``output.log`` (decoded lossily)."""
    target = run_dir / "output.log"
    if not target.is_file():
        return ""
    try:
        size = target.stat().st_size
        with target.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()  # drop a partial first line after the seek
            data = fh.read()
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _run_dir_for(kind: str, run_id: str) -> Path | None:
    candidate = RUNS_DIR / kind / run_id
    return candidate if candidate.is_dir() else None


def get_run(kind: str, run_id: str, *, with_output: bool = True) -> dict[str, Any] | None:
    """One run's record (run.json) plus its live output tail, or None if absent."""
    run_dir = _run_dir_for(kind, run_id)
    if run_dir is None:
        return None
    record = read_run(run_dir)
    record.setdefault("kind", kind)
    record.setdefault("run_id", run_id)
    if with_output:
        record["output_tail"] = read_output_tail(run_dir)
    return record


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    """Newest-first run records across all kinds (no output bytes — cheap)."""
    if not RUNS_DIR.is_dir():
        return []
    rows: list[tuple[str, dict[str, Any]]] = []
    for kind_dir in RUNS_DIR.iterdir():
        if not kind_dir.is_dir():
            continue
        for run_dir in kind_dir.iterdir():
            if not run_dir.is_dir():
                continue
            record = read_run(run_dir)
            record.setdefault("kind", kind_dir.name)
            record.setdefault("run_id", run_dir.name)
            # Sort key: the run id is timestamp-sortable within a kind; pair it
            # with started_at so cross-kind ordering is by wall-clock start.
            rows.append((str(record.get("started_at") or run_dir.name), record))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [record for _, record in rows[:limit]]


# ----------------------------------------------------------- spawn / watch


def active_run() -> dict[str, Any] | None:
    """The in-flight run's ``{kind, run_id}``, or None if idle."""
    with _LOCK:
        if _ACTIVE is not None and _ACTIVE["proc"].poll() is None:
            return {"kind": _ACTIVE["kind"], "run_id": _ACTIVE["run_id"]}
    return None


def start_run(
    kind: str, argv_tail: list[str], *, env_overrides: dict[str, str] | None = None
) -> dict[str, Any]:
    """Spawn ``launcher.py <argv_tail>`` detached, capturing output. Single-flight.

    Returns ``{kind, run_id}``. Raises :class:`RunBusyError` if a run is already
    in flight. ``env_overrides`` (e.g. ``WR_DB_PATH``) is layered onto the child
    env so the spawned CLI targets exactly the DB the webapp is reading.
    """
    global _ACTIVE
    with _LOCK:
        if _ACTIVE is not None and _ACTIVE["proc"].poll() is None:
            raise RunBusyError(
                f"a {_ACTIVE['kind']} run ({_ACTIVE['run_id']}) is still in progress"
            )

        run_id = new_run_id()
        run_dir = _new_run_dir(kind, run_id)
        rid = run_dir.name
        write_run_json(
            run_dir,
            kind=kind,
            run_id=rid,
            status="running",
            started_at=_now_iso(),
            argv=argv_tail,
        )

        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
        if env_overrides:
            env.update(env_overrides)

        log_fh: IO[bytes] = (run_dir / "output.log").open("wb")
        try:
            proc = subprocess.Popen(
                [_python(), str(_LAUNCHER), *argv_tail],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                env=env,
                creationflags=_creationflags(),
                close_fds=True,
            )
        except OSError as exc:
            log_fh.close()
            write_run_json(
                run_dir, status="failed", finished_at=_now_iso(), error=f"spawn failed: {exc}"
            )
            raise

        write_run_json(run_dir, pid=proc.pid)
        _ACTIVE = {"kind": kind, "run_id": rid, "dir": run_dir, "proc": proc}
        threading.Thread(
            target=_watch, args=(rid, run_dir, proc, log_fh), daemon=True
        ).start()
        return {"kind": kind, "run_id": rid}


def _watch(run_id: str, run_dir: Path, proc: subprocess.Popen[bytes], log_fh: IO[bytes]) -> None:
    """Wait for the run to exit, then finalize run.json + extract the result."""
    global _ACTIVE
    try:
        exit_code = proc.wait()
    finally:
        log_fh.close()
    result = parse_result(read_output_tail(run_dir))
    fields: dict[str, Any] = {
        "status": "completed" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "finished_at": _now_iso(),
    }
    if result is not None:
        fields["result"] = result
        # The CLI's DB run id, when the verb records one — the merge key that
        # lets the runs list show one entry per execution across stores (#163).
        if isinstance(result.get("run_id"), int):
            fields["db_run_id"] = result["run_id"]
    write_run_json(run_dir, **fields)
    with _LOCK:
        if _ACTIVE is not None and _ACTIVE["run_id"] == run_id:
            _ACTIVE = None


def kill_run(kind: str, run_id: str) -> bool:
    """Terminate the run if it is the active, still-running one. True if signalled.

    The CLI does its work in-process (no grandchildren), so terminating the
    launched interpreter is sufficient — no process-tree walk needed.
    """
    with _LOCK:
        active = _ACTIVE
        if (
            active is not None
            and active["kind"] == kind
            and active["run_id"] == run_id
            and active["proc"].poll() is None
        ):
            active["proc"].terminate()
            return True
    return False
