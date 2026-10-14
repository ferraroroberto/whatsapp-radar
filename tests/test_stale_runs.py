"""Runs stranded at ``running`` are reconciled to ``abandoned`` (#371).

Root cause: ``_cmd_family_check`` opens a ``running`` row, then only finalizes it
for ``FileNotFoundError`` / ``RuntimeError``. Any other exception, an interrupt,
or a process killed outright (Job timeout, restart, reboot) left the row at
``running`` forever, and nothing ever looked at it again.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.cli import main as cli
from app.webapp.server import create_app
from src.db import store
from src.webapp_config import WebappConfig

NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


def _seed_running(
    conn: sqlite3.Connection, *, kind: str, age: timedelta, started_at: str | None = None
) -> int:
    run_id = store.start_run(conn, mode="live", kind=kind)
    stamp = started_at or (NOW - age).isoformat(timespec="seconds")
    conn.execute("UPDATE review_runs SET started_at = ? WHERE id = ?", (stamp, run_id))
    conn.commit()
    return run_id


def _status(conn: sqlite3.Connection, run_id: int) -> str:
    row = store.review_run(conn, run_id)
    assert row is not None
    return str(row["status"])


# --- the exit paths that strand a row ----------------------------------------


def _patch_traffic_runner(monkeypatch: pytest.MonkeyPatch, exc: BaseException) -> None:
    import src.family.traffic_check as traffic_check

    def boom(config: Any, *, now: Any, dry_run: bool) -> dict[str, Any]:
        raise exc

    monkeypatch.setattr(traffic_check, "run_traffic_check", boom)


def test_unexpected_error_does_not_strand_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-Runtime/FileNotFound error (a network blip, a bad payload) is a failure."""
    db = tmp_path / "family.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))
    _patch_traffic_runner(monkeypatch, ConnectionError("routes api reset"))

    with pytest.raises(ConnectionError):
        cli.main(["traffic-check"])

    conn = store.connect(db)
    try:
        row = store.list_review_runs(conn, 10)[0]
        assert row["status"] == "failed"
        assert "routes api reset" in row["error"]
    finally:
        conn.close()


def test_interrupted_check_is_abandoned_not_failed_or_stranded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C / a termination signal outcome is unobserved: its own state, not an error."""
    db = tmp_path / "family.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))
    _patch_traffic_runner(monkeypatch, KeyboardInterrupt())

    with pytest.raises(KeyboardInterrupt):
        cli.main(["traffic-check"])

    conn = store.connect(db)
    try:
        row = store.list_review_runs(conn, 10)[0]
        assert row["status"] == "abandoned"
        assert row["completed_at"] is None
    finally:
        conn.close()


def test_a_killed_check_is_swept_up_by_the_next_fire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hard kill cannot be caught in-process; the next check reconciles its row."""
    db = tmp_path / "family.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))
    conn = store.connect(db)
    # Real-clock stamp: the CLI sweeps against "now", not this module's NOW.
    old = _seed_running(
        conn, kind="traffic-check", age=timedelta(0),
        started_at=(datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="seconds"),
    )
    conn.close()

    import src.family.traffic_check as traffic_check

    monkeypatch.setattr(
        traffic_check, "run_traffic_check",
        lambda config, *, now, dry_run: {"kind": "traffic-check", "status": "ok", "alerts": 0},
    )
    # Dry run: skips the cadence self-skip, which would otherwise be exercised here.
    assert cli.main(["traffic-check", "--dry-run"]) == 0

    conn = store.connect(db)
    try:
        assert _status(conn, old) == "abandoned"
    finally:
        conn.close()


# --- the reconcile rule ------------------------------------------------------


def test_reconcile_abandons_only_runs_the_rule_can_establish(conn: sqlite3.Connection) -> None:
    stale = _seed_running(conn, kind="traffic-check", age=timedelta(hours=5))
    fresh = _seed_running(conn, kind="traffic-check", age=timedelta(minutes=2))
    stale_scan = _seed_running(conn, kind="scan", age=timedelta(days=3))
    garbled = _seed_running(
        conn, kind="traffic-check", age=timedelta(0), started_at="not-a-timestamp"
    )
    done = _seed_running(conn, kind="traffic-check", age=timedelta(hours=9))
    store.finish_run_summary(conn, done, "completed", None)

    assert store.reconcile_stale_runs(conn, now=NOW) == [stale]

    assert _status(conn, stale) == "abandoned"
    assert _status(conn, fresh) == "running"  # may be a live check
    assert _status(conn, stale_scan) == "running"  # no bound for scans: unknown, untouched
    assert _status(conn, garbled) == "running"  # unparseable: unknown, untouched
    assert _status(conn, done) == "completed"
    row = store.review_run(conn, stale)
    assert row is not None
    assert row["completed_at"] is None
    assert "abandoned" in row["error"]


def test_reconcile_is_idempotent(conn: sqlite3.Connection) -> None:
    _seed_running(conn, kind="calendar-scan", age=timedelta(hours=2))
    assert len(store.reconcile_stale_runs(conn, now=NOW)) == 1
    assert store.reconcile_stale_runs(conn, now=NOW) == []


def test_reconcile_accepts_naive_and_fractional_timestamps(conn: sqlite3.Connection) -> None:
    older = _seed_running(
        conn, kind="traffic-check", age=timedelta(0), started_at="2026-10-14T08:00:00.123456"
    )
    assert store.reconcile_stale_runs(conn, now=NOW) == [older]


def test_abandon_never_overwrites_a_run_that_finished_meanwhile(
    conn: sqlite3.Connection,
) -> None:
    run_id = _seed_running(conn, kind="traffic-check", age=timedelta(hours=2))
    store.finish_run_summary(conn, run_id, "completed", None)
    assert store.abandon_run(conn, run_id, "late") is False
    assert _status(conn, run_id) == "completed"


def test_each_reconciled_run_is_logged_at_info(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    a = _seed_running(conn, kind="traffic-check", age=timedelta(hours=2))
    b = _seed_running(conn, kind="traffic-check", age=timedelta(hours=3))
    with caplog.at_level(logging.INFO, logger="src.db.runs"):
        store.reconcile_stale_runs(conn, now=NOW)
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert len(messages) == 2
    assert f"run {a} " in messages[0] and f"run {b} " in messages[1]
    assert all("abandoned" in m for m in messages)


# --- webapp startup + UI contract --------------------------------------------


def test_webapp_startup_reconciles_stale_rows(tmp_path: Path) -> None:
    db = tmp_path / "startup.sqlite3"
    conn = store.connect(db)
    stale = _seed_running(
        conn, kind="traffic-check", age=timedelta(0),
        started_at=(datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds"),
    )
    conn.close()

    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    with TestClient(app, client=("127.0.0.1", 5555)) as client:
        body = client.get("/api/audit/runs?scope=traffic").json()

    rows = [r for r in body["runs"] if r["id"] == stale]
    assert [r["status"] for r in rows] == ["abandoned"]


def test_activity_view_gives_abandoned_its_own_badge() -> None:
    js = (Path(__file__).resolve().parent.parent / "app/webapp/static/activity.js").read_text(
        encoding="utf-8"
    )
    assert "'abandoned'" in js and "statusChip('Abandoned'" in js
