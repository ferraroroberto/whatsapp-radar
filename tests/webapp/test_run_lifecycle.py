"""The run lifecycle is declared once and every terminal write goes through one guarded writer.

Root cause of the stuck-``running`` defect: ``scan`` / ``process`` opened a
``running`` row and only finalized it on their own happy and offline-abort paths,
so a raise or a kill left it at ``running`` forever, and the status and the funnel
were two separate commits.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.cli import main as cli
from src.analysis.pipeline import scan
from src.db import runs as runs_module
from src.db import store
from src.process_liveness import pid_state
from src.subprocess_flags import NO_WINDOW
from tests.helpers import chat_id_by_source, review_config

REPO = Path(__file__).resolve().parents[2]

DEAD_PID = 4_000_001
LIVE_PID = 4_000_002
UNKNOWN_PID = 4_000_003


def _status(conn: sqlite3.Connection, run_id: int) -> str:
    row = store.review_run(conn, run_id)
    assert row is not None
    return str(row["status"])


def _set_owner(conn: sqlite3.Connection, run_id: int, pid: int | None) -> None:
    conn.execute("UPDATE review_runs SET owner_pid = ? WHERE id = ?", (pid, run_id))
    conn.commit()


# --- the declared lifecycle ---------------------------------------------------


def test_every_status_is_declared_and_only_running_may_move() -> None:
    terminal = {"completed", "completed_with_errors", "failed", "abandoned"}
    assert set(runs_module.TERMINAL_STATUSES) == terminal
    assert set(runs_module.RUN_STATUSES) == terminal | {"running"}
    assert runs_module.LEGAL_TRANSITIONS["running"] == runs_module.TERMINAL_STATUSES
    for status in terminal:
        assert runs_module.LEGAL_TRANSITIONS[status] == frozenset()


def test_no_run_status_literal_is_left_in_the_pipeline_or_the_cli() -> None:
    """The pipeline and the CLI spell run statuses through the declared constants."""
    literal = re.compile(r"""["'](running|completed|completed_with_errors|abandoned)["']""")
    for rel in ("src/analysis/pipeline.py", "app/cli/main.py"):
        lines = (REPO / rel).read_text(encoding="utf-8").splitlines()
        hits = [
            f"{rel}:{n}: {line.strip()}"
            for n, line in enumerate(lines, 1)
            if literal.search(line) and not line.lstrip().startswith("#")
        ]
        assert hits == []


# --- the guarded finaliser ----------------------------------------------------


@pytest.mark.parametrize("first", ["abandoned", "completed"])
def test_finalizer_refuses_to_overwrite_a_terminal_row(
    conn: sqlite3.Connection, first: str, caplog: pytest.LogCaptureFixture
) -> None:
    run_id = store.start_run(conn, mode="live")
    assert store.finalize_run(conn, run_id, first, error="first") is True
    before = dict(store.review_run(conn, run_id) or {})

    with caplog.at_level(logging.INFO, logger="src.db.runs"):
        moved = store.finalize_run(
            conn, run_id, "completed_with_errors", error="late", chats_reviewed=9,
            funnel=store.RunFunnel(
                chats_synced=1, messages_synced=1, chats_monitored=1, stage1_passed=1,
                stage2_llm_calls=1, actionable=1, notification_status="sent",
            ),
        )

    assert moved is False
    assert dict(store.review_run(conn, run_id) or {}) == before
    assert any(f"already {first}" in r.getMessage() for r in caplog.records)


def test_finalizer_rejects_a_status_that_is_not_terminal(conn: sqlite3.Connection) -> None:
    run_id = store.start_run(conn, mode="live")
    with pytest.raises(ValueError):
        store.finalize_run(conn, run_id, "running")
    with pytest.raises(ValueError):
        store.finalize_run(conn, run_id, "done")
    assert _status(conn, run_id) == "running"


def test_abandoned_leaves_completed_at_empty_and_the_rest_stamp_it(
    conn: sqlite3.Connection,
) -> None:
    gone = store.start_run(conn, mode="live")
    done = store.start_run(conn, mode="live")
    store.finalize_run(conn, gone, "abandoned", error="x")
    store.finalize_run(conn, done, "completed")
    assert (store.review_run(conn, gone) or {})["completed_at"] is None
    assert (store.review_run(conn, done) or {})["completed_at"] is not None


def test_status_and_funnel_land_in_one_statement(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A crash between two commits left ``completed`` with a NULL ``notification_status``."""
    conn = ingested_conn
    first = conn.execute("SELECT source_chat_id FROM chats ORDER BY id LIMIT 1").fetchone()
    store.set_chat_status(conn, chat_id_by_source(conn, first["source_chat_id"]), "monitored")
    statements: list[str] = []
    conn.set_trace_callback(statements.append)
    try:
        outcome = scan(conn, review_config(tmp_path), mode="dry_run")
    finally:
        conn.set_trace_callback(None)

    updates = [s for s in statements if s.lstrip().upper().startswith("UPDATE REVIEW_RUNS")]
    assert len(updates) == 1
    assert "notification_status" in updates[0] and "status" in updates[0]
    row = store.review_run(conn, outcome.run_id)
    assert row is not None
    assert row["status"] in {"completed", "completed_with_errors"}
    assert row["notification_status"] is not None


def test_a_normal_run_records_the_same_values_plus_its_owner(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    outcome = scan(ingested_conn, review_config(tmp_path), mode="dry_run")
    row = store.review_run(ingested_conn, outcome.run_id)
    assert row is not None
    assert row["status"] == "completed"
    assert row["completed_at"] is not None
    assert row["notification_status"] == outcome.notification_status
    assert row["owner_pid"] == os.getpid()


# --- exit handling for message runs ------------------------------------------


def _patch_body(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(store, "monitored_chats", boom)


def _only_run(db: Path) -> sqlite3.Row:
    conn = store.connect(db)
    try:
        rows = store.list_review_runs(conn, 10)
        assert len(rows) == 1
        return rows[0]
    finally:
        conn.close()


@pytest.mark.parametrize("argv", [["scan", "--dry-run"], ["review", "--dry-run"]])
def test_a_message_run_that_raises_is_recorded_failed(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "runs.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))
    _patch_body(monkeypatch, ConnectionError("hub reset"))

    with pytest.raises(ConnectionError):
        cli.main(argv)

    row = _only_run(db)
    assert row["status"] == "failed"
    assert "ConnectionError" in row["error"] and "hub reset" in row["error"]
    assert row["completed_at"] is not None


@pytest.mark.parametrize("argv", [["scan", "--dry-run"], ["review", "--dry-run"]])
def test_an_interrupted_message_run_is_recorded_abandoned(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "runs.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))
    _patch_body(monkeypatch, KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        cli.main(argv)

    row = _only_run(db)
    assert row["status"] == "abandoned"
    assert row["completed_at"] is None


def test_a_failure_after_the_run_finalised_does_not_rewrite_it(
    ingested_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit handler goes through the same guard as every other terminal write."""
    conn = ingested_conn
    real = store.finish_run

    def finish_then_raise(*args: Any, **kwargs: Any) -> bool:
        real(*args, **kwargs)
        raise RuntimeError("after finalise")

    monkeypatch.setattr(store, "finish_run", finish_then_raise)
    with pytest.raises(RuntimeError):
        scan(conn, review_config(tmp_path), mode="dry_run")
    row = store.list_review_runs(conn, 1)[0]
    assert row["status"] == "completed"
    assert row["error"] is None


# --- owner pid ----------------------------------------------------------------


def _fake_pid_state(monkeypatch: pytest.MonkeyPatch) -> None:
    states = {DEAD_PID: False, LIVE_PID: True, UNKNOWN_PID: None}
    monkeypatch.setattr(runs_module, "pid_state", lambda pid: states.get(pid, True))


def test_reconcile_abandons_a_running_row_whose_owner_is_gone(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _fake_pid_state(monkeypatch)
    dead_scan = store.start_run(conn, mode="live", kind="scan")
    dead_process = store.start_run(conn, mode="process", kind="process")
    dead_family = store.start_run(conn, mode="live", kind="traffic-check")
    live = store.start_run(conn, mode="live", kind="scan")
    unknown = store.start_run(conn, mode="live", kind="scan")
    legacy = store.start_run(conn, mode="live", kind="scan")
    finished = store.start_run(conn, mode="live", kind="scan")
    for run_id, pid in (
        (dead_scan, DEAD_PID), (dead_process, DEAD_PID), (dead_family, DEAD_PID),
        (live, LIVE_PID), (unknown, UNKNOWN_PID), (legacy, None), (finished, DEAD_PID),
    ):
        _set_owner(conn, run_id, pid)
    store.finalize_run(conn, finished, "completed")

    with caplog.at_level(logging.INFO, logger="src.db.runs"):
        reconciled = store.reconcile_stale_runs(conn)

    assert reconciled == [dead_scan, dead_process, dead_family]
    for run_id in reconciled:
        assert _status(conn, run_id) == "abandoned"
    assert _status(conn, live) == "running"
    assert _status(conn, unknown) == "running"  # the probe could not tell: not folded into a pass
    assert _status(conn, legacy) == "running"  # no owner recorded: the pid rule never applies
    assert _status(conn, finished) == "completed"
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(infos) == 3 and all("abandoned" in m for m in infos)


def test_a_run_sweeps_up_the_dead_run_before_it_opens_its_own_row(
    ingested_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_pid_state(monkeypatch)
    killed = store.start_run(ingested_conn, mode="live", kind="scan")
    _set_owner(ingested_conn, killed, DEAD_PID)

    scan(ingested_conn, review_config(tmp_path), mode="dry_run")

    assert _status(ingested_conn, killed) == "abandoned"


def test_the_webapp_startup_sweep_uses_the_owner_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from starlette.testclient import TestClient

    from app.webapp.server import create_app
    from src.webapp_config import WebappConfig

    _fake_pid_state(monkeypatch)
    db = tmp_path / "startup.sqlite3"
    conn = store.connect(db)
    killed = store.start_run(conn, mode="live", kind="scan")
    _set_owner(conn, killed, DEAD_PID)
    conn.close()

    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    with TestClient(app, client=("127.0.0.1", 5555)):
        pass

    conn = store.connect(db)
    try:
        assert _status(conn, killed) == "abandoned"
    finally:
        conn.close()


def test_start_run_records_the_owning_process(conn: sqlite3.Connection) -> None:
    run_id = store.start_run(conn, mode="live", kind="traffic-check")
    row = store.review_run(conn, run_id)
    assert row is not None and row["owner_pid"] == os.getpid()


def test_a_legacy_row_without_an_owner_survives_the_migration(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite3"
    raw = sqlite3.connect(db)
    raw.executescript(
        "CREATE TABLE review_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,"
        " completed_at TEXT, status TEXT NOT NULL DEFAULT 'running',"
        " chats_reviewed INTEGER NOT NULL DEFAULT 0, error TEXT);"
        "INSERT INTO review_runs (started_at, status)"
        " VALUES ('2026-01-01T00:00:00+00:00', 'running');"
    )
    raw.commit()
    raw.close()

    conn = store.connect(db)
    try:
        row = store.review_run(conn, 1)
        assert row is not None and row["owner_pid"] is None and row["status"] == "running"
        assert store.reconcile_stale_runs(conn) == []
        assert _status(conn, 1) == "running"
    finally:
        conn.close()


# --- the pid probe ------------------------------------------------------------


def test_pid_state_is_three_valued() -> None:
    assert pid_state(os.getpid()) is True
    assert pid_state(0) is None
    assert pid_state(-5) is None


def test_pid_state_proves_a_finished_process_gone() -> None:
    done = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", "import os; print(os.getpid())"],
        capture_output=True, text=True, timeout=60, check=True, creationflags=NO_WINDOW,
    )
    assert pid_state(int(done.stdout.strip())) is False


# --- the UI contract ----------------------------------------------------------


def test_activity_chips_completed_with_errors_and_pending_is_not_running() -> None:
    static = REPO / "app/webapp/static"
    activity = (static / "activity.js").read_text(encoding="utf-8")
    assert "'completed_with_errors'" in activity
    assert "statusChip('With errors', 'attention')" in activity
    for name in ("execution.js", "family.js"):
        assert "'pending'" not in (static / name).read_text(encoding="utf-8")
