"""Unified run recording across scan / process / family checks (#163).

Every run — message pipeline or family check, CLI-launched or webapp-launched —
lands in the one DB run store with a kind, a UTC timestamp, and (for family
checks) the structured payload, so the Audit tab and the Run tab read one truth.
Offline throughout: the family runners are stubbed, no network, no Google.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.cli import main as cli
from app.webapp import runs as webapp_runs
from app.webapp.server import create_app
from src.db import store
from src.db.connection import _now
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)

# UTC ISO, seconds precision, explicit offset — parseable by every browser
# Date(); microsecond fractions were the old, fragile format (#163).
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


# --- timestamp discipline ---------------------------------------------------

def test_db_and_webapp_timestamps_share_one_format() -> None:
    assert _TS.match(_now())
    assert _TS.match(webapp_runs._now_iso())


# --- store: kinds + summary -------------------------------------------------

def test_start_run_records_kind(conn: sqlite3.Connection) -> None:
    scan_id = store.start_run(conn, mode="live")
    family_id = store.start_run(conn, mode="dry_run", kind="traffic-check")
    rows = {int(r["id"]): r for r in store.list_review_runs(conn, 10)}
    assert rows[scan_id]["kind"] == "scan"
    assert rows[family_id]["kind"] == "traffic-check"
    assert rows[family_id]["mode"] == "dry_run"
    assert _TS.match(rows[family_id]["started_at"])


def test_finish_run_summary_persists_payload(conn: sqlite3.Connection) -> None:
    run_id = store.start_run(conn, mode="live", kind="calendar-scan")
    payload = {"kind": "calendar-scan", "status": "ok", "conflicts": [], "run_id": run_id}
    store.finish_run_summary(conn, run_id, "completed", json.dumps(payload))
    row = store.review_run(conn, run_id)
    assert row is not None
    assert row["status"] == "completed"
    assert row["completed_at"] is not None
    assert json.loads(row["summary_json"]) == payload


def test_message_run_queries_skip_family_kinds(conn: sqlite3.Connection) -> None:
    scan_id = store.start_run(conn, mode="live")
    store.finish_run(conn, scan_id, "completed", chats_reviewed=0)
    family_id = store.start_run(conn, mode="live", kind="traffic-check")
    store.finish_run_summary(conn, family_id, "completed", "{}")

    # `wr notify` must never pick a family run as "the latest digest" and the
    # dashboard's scan counters must not count family checks.
    assert store.latest_run_id(conn) == scan_id
    last = store.last_run(conn)
    assert last is not None and int(last["id"]) == scan_id
    assert store.count_runs(conn) == 1


def test_migration_backfills_kind_for_legacy_review_rows(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite3"
    raw = sqlite3.connect(db)
    raw.execute(
        "CREATE TABLE review_runs ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, "
        "completed_at TEXT, status TEXT NOT NULL DEFAULT 'running', "
        "chats_reviewed INTEGER NOT NULL DEFAULT 0, error TEXT)"
    )
    raw.execute(
        "INSERT INTO review_runs (started_at, status) "
        "VALUES ('2026-01-01T00:00:00+00:00', 'completed')"
    )
    raw.commit()
    raw.close()

    conn = store.connect(db)
    try:
        rows = store.list_review_runs(conn, 10)
        # The legacy row had no mode → migrated mode defaults to 'review',
        # which the backfill maps to the process kind.
        assert rows[0]["kind"] == "process"
        assert rows[0]["summary_json"] is None
    finally:
        conn.close()


# --- CLI: family checks record runs -----------------------------------------

def _run_family_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
    payload: dict[str, Any],
) -> tuple[int, Path]:
    db = tmp_path / "family.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))

    def fake_runner(config: Any, *, now: Any, dry_run: bool) -> dict[str, Any]:
        return dict(payload, dry_run=dry_run)

    import src.family.calendar_scan as calendar_scan
    import src.family.traffic_check as traffic_check

    monkeypatch.setattr(calendar_scan, "run_calendar_scan", fake_runner)
    monkeypatch.setattr(traffic_check, "run_traffic_check", fake_runner)
    return cli.main(argv), db


def test_cli_calendar_scan_records_a_run_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = {"kind": "calendar-scan", "status": "ok", "conflicts": [], "unknown_locations": []}
    rc, db = _run_family_cli(
        tmp_path, monkeypatch, ["calendar-scan", "--dry-run"], payload
    )
    assert rc == 0
    conn = store.connect(db)
    try:
        rows = store.list_review_runs(conn, 10)
        assert len(rows) == 1
        row = rows[0]
        assert row["kind"] == "calendar-scan"
        assert row["mode"] == "dry_run"
        assert row["status"] == "completed"
        summary = json.loads(row["summary_json"])
        assert summary["status"] == "ok"
        assert summary["run_id"] == int(row["id"])
    finally:
        conn.close()
    # The result sentinel carries the DB run id back to the webapp watcher.
    out = capsys.readouterr().out
    assert '"run_id"' in out


def test_cli_traffic_check_failure_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "family.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))

    def boom(config: Any, *, now: Any, dry_run: bool) -> dict[str, Any]:
        raise RuntimeError("routes api down")

    import src.family.traffic_check as traffic_check

    monkeypatch.setattr(traffic_check, "run_traffic_check", boom)
    assert cli.main(["traffic-check"]) == 1
    conn = store.connect(db)
    try:
        row = store.list_review_runs(conn, 10)[0]
        assert row["kind"] == "traffic-check"
        assert row["status"] == "failed"
        assert "routes api down" in row["error"]
    finally:
        conn.close()


# --- API: unified visibility ------------------------------------------------

def _client(db: Path) -> TestClient:
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    return TestClient(app, client=LOOPBACK)


def _seed_family_run(db: Path, *, kind: str = "traffic-check", mode: str = "live") -> int:
    conn = store.connect(db)
    try:
        run_id = store.start_run(conn, mode=mode, kind=kind)
        payload = {
            "kind": kind,
            "status": "ok",
            "checked": [{"person": "dad", "event": "School run"}],
            "alerts": 1,
            "run_id": run_id,
        }
        store.finish_run_summary(conn, run_id, "completed", json.dumps(payload))
        return run_id
    finally:
        conn.close()


def test_audit_lists_family_runs_with_kind_and_summary(tmp_path: Path) -> None:
    db = tmp_path / "audit.sqlite3"
    run_id = _seed_family_run(db)
    with _client(db) as client:
        body = client.get("/api/audit/runs").json()
        filtered = client.get("/api/audit/runs", params={"kind": "traffic-check"}).json()
        empty = client.get("/api/audit/runs", params={"kind": "calendar-scan"}).json()
        detail = client.get(f"/api/audit/runs/{run_id}").json()

    run = body["runs"][0]
    assert run["kind"] == "traffic-check"
    assert run["summary"]["alerts"] == 1
    assert [r["id"] for r in filtered["runs"]] == [run_id]
    assert empty["runs"] == []
    assert detail["run"]["summary"]["checked"][0]["event"] == "School run"
    assert detail["traces"] == []


def test_execution_runs_merges_db_only_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webapp_runs, "RUNS_DIR", tmp_path / "runs")
    db = tmp_path / "exec.sqlite3"
    run_id = _seed_family_run(db, mode="dry_run")

    with _client(db) as client:
        body = client.get("/api/execution/runs").json()
        detail = client.get(f"/api/execution/runs/traffic-check/db-{run_id}").json()

    assert len(body["runs"]) == 1
    record = body["runs"][0]
    assert record["kind"] == "traffic-check"
    assert record["run_id"] == f"db-{run_id}"
    assert record["origin"] == "db"
    assert record["mode"] == "dry_run"
    assert record["status"] == "completed"
    assert detail["run"]["result"]["alerts"] == 1
    assert "no captured output" in detail["run"]["output_tail"]


def test_execution_runs_dedups_webapp_launched_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webapp_runs, "RUNS_DIR", tmp_path / "runs")
    db = tmp_path / "exec.sqlite3"

    # A DB scan row + its filesystem record carrying the db run id (as _watch
    # writes after parsing the result sentinel) must merge to ONE entry, with
    # the DB's clock and mode winning.
    conn = store.connect(db)
    try:
        run_id = store.start_run(conn, mode="dry_run", params_json="{}")
        store.finish_run(conn, run_id, "completed", chats_reviewed=0)
        db_started = store.review_run(conn, run_id)["started_at"]
    finally:
        conn.close()

    run_dir = webapp_runs._new_run_dir("scan", "20260101T000000")
    webapp_runs.write_run_json(
        run_dir,
        kind="scan",
        run_id="20260101T000000",
        status="completed",
        started_at="2026-01-01T00:00:00+00:00",
        argv=["scan", "--dry-run"],
        db_run_id=run_id,
    )

    with _client(db) as client:
        body = client.get("/api/execution/runs").json()

    assert len(body["runs"]) == 1
    record = body["runs"][0]
    assert record["run_id"] == "20260101T000000"  # filesystem record kept (output)
    assert record["db_run_id"] == run_id
    assert record["started_at"] == db_started  # DB clock is authoritative
    assert record["mode"] == "dry_run"


def test_family_endpoint_reads_db_runs(tmp_path: Path) -> None:
    db = tmp_path / "family.sqlite3"
    _seed_family_run(db, kind="traffic-check")
    with _client(db) as client:
        body = client.get("/api/family").json()
    assert len(body["runs"]) == 1
    run = body["runs"][0]
    assert run["kind"] == "traffic-check"
    assert run["status"] == "completed"
    assert run["checked"] == 1
    assert run["alerts"] == 1
