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

from src.paths import PROJECT_ROOT
from src.runresult import parse_result
from src.subprocess_flags import NO_WINDOW_DETACHED

RUNS_DIR = PROJECT_ROOT / "webapp" / "runs"
_LAUNCHER = PROJECT_ROOT / "launcher.py"

#: Set on a child this module captures, so a CLI that would otherwise open its
#: own run record (:mod:`app.cli.runlog`, #233) knows to stand down — one
#: process must never be captured twice.
CAPTURED_ENV_VAR = "WR_RUN_CAPTURED"
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


def now_iso() -> str:
    """The one clock every run record is stamped with.

    UTC with an explicit offset — the same discipline as the DB store, so the
    same run can never show two different times across surfaces (#163). Public
    because the CLI writes records of its own (:mod:`app.cli.runlog`, #233) and
    must share this clock rather than pick a second one.
    """
    return datetime.now(UTC).isoformat(timespec="seconds")


def _python() -> str:
    """The interpreter to launch the CLI with — this repo's venv, else current."""
    candidate = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    return str(candidate) if candidate.is_file() else sys.executable


# ----------------------------------------------------------- run records


def new_run_id() -> str:
    """A sortable, filesystem-safe run id (second resolution)."""
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def new_run_dir(kind: str, run_id: str) -> Path:
    """Create ``webapp/runs/<kind>/<run_id>/``, disambiguating same-second ids.

    ``mkdir`` *is* the claim: a check-then-create would race, and since #233 two
    processes really can land in the same second (a scheduled App Launcher Job
    firing while the operator launches from the webapp), where before only the
    single-flight webapp created these.
    """
    base = RUNS_DIR / kind
    base.mkdir(parents=True, exist_ok=True)
    target = base / run_id
    n = 2
    while True:
        try:
            target.mkdir()
            return target
        except FileExistsError:
            target = base / f"{run_id}-{n}"
            n += 1


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
    """Newest-first run records across all kinds (no output bytes — cheap).

    Bounded per kind: run-id directory names are timestamp-sortable, so the
    newest ``limit`` names of each kind are the only candidates that can survive
    the final cross-kind truncation — reading the rest would be wasted I/O.

    This matters since #233, because the CLI now writes a record too, and the
    volume is real: App Launcher fires ``traffic-check`` every 5 minutes against
    a 30-minute in-process cadence, so ~288 records a day land here (~240 of them
    self-skips) and this function runs on every Execution-tab poll. Deliberately
    a read bound and not a cleanup — nothing here deletes a run (retention, and
    that fire-vs-cadence mismatch, are #234).
    """
    if not RUNS_DIR.is_dir():
        return []
    rows: list[tuple[str, dict[str, Any]]] = []
    for kind_dir in sorted(RUNS_DIR.iterdir()):
        if not kind_dir.is_dir():
            continue
        names = sorted(
            (entry.name for entry in kind_dir.iterdir() if entry.is_dir()), reverse=True
        )
        for name in names[:limit]:
            run_dir = kind_dir / name
            record = read_run(run_dir)
            record.setdefault("kind", kind_dir.name)
            record.setdefault("run_id", name)
            # Sort key: the run id is timestamp-sortable within a kind; pair it
            # with started_at so cross-kind ordering is by wall-clock start.
            rows.append((str(record.get("started_at") or name), record))
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
        run_dir = new_run_dir(kind, run_id)
        rid = run_dir.name
        write_run_json(
            run_dir,
            kind=kind,
            run_id=rid,
            status="running",
            started_at=now_iso(),
            argv=argv_tail,
            origin="webapp",
        )

        # CAPTURED_ENV_VAR tells the child's own capture layer to stand down:
        # this run's output is already being written to output.log below, and a
        # second record for the same process would double-list it (#233).
        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            CAPTURED_ENV_VAR: "1",
        }
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
                creationflags=NO_WINDOW_DETACHED,
                close_fds=True,
            )
        except OSError as exc:
            log_fh.close()
            write_run_json(
                run_dir, status="failed", finished_at=now_iso(), error=f"spawn failed: {exc}"
            )
            raise

        write_run_json(run_dir, pid=proc.pid)
        _ACTIVE = {"kind": kind, "run_id": rid, "dir": run_dir, "proc": proc}
        threading.Thread(
            target=_watch, args=(rid, run_dir, proc, log_fh), daemon=True
        ).start()
        return {"kind": kind, "run_id": rid}


def finalize_record(run_dir: Path, exit_code: int | None) -> None:
    """Write a finished run's terminal ``run.json`` fields.

    The one definition of what "finished" looks like on disk, shared by the
    webapp's watcher thread and the CLI's own capture (:mod:`app.cli.runlog`,
    #233) — two writers of the same record must not drift in shape.

    ``exit_code`` is ``None`` when the process died without yielding one (an
    exception escaping the CLI); that is a failure, and the field is simply
    omitted rather than guessed at.
    """
    result = parse_result(read_output_tail(run_dir))
    fields: dict[str, Any] = {
        "status": "completed" if exit_code == 0 else "failed",
        "exit_code": exit_code,
        "finished_at": now_iso(),
    }
    if result is not None:
        fields["result"] = result
        # The CLI's DB run id, when the verb records one — the merge key that
        # lets the runs list show one entry per execution across stores (#163).
        if isinstance(result.get("run_id"), int):
            fields["db_run_id"] = result["run_id"]
    write_run_json(run_dir, **fields)


def _watch(run_id: str, run_dir: Path, proc: subprocess.Popen[bytes], log_fh: IO[bytes]) -> None:
    """Wait for the run to exit, then finalize run.json + extract the result."""
    global _ACTIVE
    try:
        exit_code = proc.wait()
    finally:
        log_fh.close()
    finalize_record(run_dir, exit_code)
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
