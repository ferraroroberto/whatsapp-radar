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


# --- CLI: traffic-check cadence self-skip (#170) -----------------------------
#
# The App Launcher job is armed at a fixed high frequency; `wr traffic-check`
# self-skips in-process when `traffic.cadence_min` hasn't elapsed since the
# last recorded traffic-check run, so a cadence edit in the UI takes effect
# with no Task Scheduler re-arm. A skip records no run row (would drown the
# Audit tab in a no-op every few minutes) but does print a log line.

def _cadence_config(tmp_path: Path, *, cadence_min: int) -> Any:
    import dataclasses

    from src.config import load_config

    base = load_config(root=tmp_path)  # empty root -> library defaults
    traffic = dataclasses.replace(base.traffic, cadence_min=cadence_min)
    return dataclasses.replace(base, traffic=traffic)


def test_cli_traffic_check_self_skips_within_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A **live** fire within the cadence window self-skips (#170)."""
    db = tmp_path / "cadence.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))

    conn = store.connect(db)
    try:
        run_id = store.start_run(conn, mode="live", kind="traffic-check")
        store.finish_run_summary(
            conn, run_id, "completed",
            json.dumps({"kind": "traffic-check", "status": "ok", "checked": [], "alerts": 0}),
        )
    finally:
        conn.close()

    cfg = _cadence_config(tmp_path, cadence_min=30)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    def never_called(config: Any, *, now: Any, dry_run: bool) -> dict[str, Any]:
        raise AssertionError("runner must not be called on a cadence self-skip")

    import src.family.traffic_check as traffic_check

    monkeypatch.setattr(traffic_check, "run_traffic_check", never_called)

    assert cli.main(["traffic-check"]) == 0

    conn = store.connect(db)
    try:
        # No second run row — a skip is not recorded (would drown the Audit tab).
        assert len(store.list_review_runs(conn, 10)) == 1
    finally:
        conn.close()

    out = capsys.readouterr().out
    assert "skipped" in out
    assert "cadence 30min not elapsed" in out


def test_cli_traffic_check_dry_run_never_cadence_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A ``--dry-run`` fire is an explicit human test pass — never cadence-skipped (#186).

    Same setup as the live self-skip test above (a live run recorded seconds
    ago, well inside the 30min cadence window) but this time the invocation is
    ``--dry-run``: the runner must actually be called, and a run row recorded.
    """
    db = tmp_path / "cadence.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))

    conn = store.connect(db)
    try:
        run_id = store.start_run(conn, mode="live", kind="traffic-check")
        store.finish_run_summary(
            conn, run_id, "completed",
            json.dumps({"kind": "traffic-check", "status": "ok", "checked": [], "alerts": 0}),
        )
    finally:
        conn.close()

    cfg = _cadence_config(tmp_path, cadence_min=30)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    payload = {"kind": "traffic-check", "status": "ok", "checked": [], "alerts": 0}
    calls: list[bool] = []

    def fake_runner(config: Any, *, now: Any, dry_run: bool) -> dict[str, Any]:
        calls.append(dry_run)
        return dict(payload, dry_run=dry_run)

    import src.family.traffic_check as traffic_check

    monkeypatch.setattr(traffic_check, "run_traffic_check", fake_runner)

    assert cli.main(["traffic-check", "--dry-run"]) == 0

    assert calls == [True]  # the runner was actually invoked, not skipped

    conn = store.connect(db)
    try:
        rows = store.list_review_runs(conn, 10)
        assert len(rows) == 2
        assert rows[0]["mode"] == "dry_run"
        assert rows[0]["status"] == "completed"
    finally:
        conn.close()

    out = capsys.readouterr().out
    assert "skipped" not in out


def test_cli_traffic_check_runs_when_cadence_elapsed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "cadence.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))

    conn = store.connect(db)
    try:
        run_id = store.start_run(conn, mode="live", kind="traffic-check")
        store.finish_run_summary(
            conn, run_id, "completed",
            json.dumps({"kind": "traffic-check", "status": "ok", "checked": [], "alerts": 0}),
        )
        # Back-date the seeded run well past any cadence.
        conn.execute(
            "UPDATE review_runs SET started_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (run_id,),
        )
        conn.commit()
    finally:
        conn.close()

    cfg = _cadence_config(tmp_path, cadence_min=30)
    monkeypatch.setattr(cli, "load_config", lambda: cfg)

    payload = {"kind": "traffic-check", "status": "ok", "checked": [], "alerts": 0}

    def fake_runner(config: Any, *, now: Any, dry_run: bool) -> dict[str, Any]:
        return dict(payload, dry_run=dry_run)

    import src.family.traffic_check as traffic_check

    monkeypatch.setattr(traffic_check, "run_traffic_check", fake_runner)

    assert cli.main(["traffic-check", "--dry-run"]) == 0

    conn = store.connect(db)
    try:
        rows = store.list_review_runs(conn, 10)
        assert len(rows) == 2
        assert rows[0]["kind"] == "traffic-check"
        assert rows[0]["status"] == "completed"
    finally:
        conn.close()


def test_last_run_started_at_returns_most_recent_of_kind(conn: sqlite3.Connection) -> None:
    assert store.last_run_started_at(conn, "traffic-check") is None
    first = store.start_run(conn, mode="live", kind="traffic-check")
    store.finish_run_summary(conn, first, "completed", "{}")
    second = store.start_run(conn, mode="live", kind="traffic-check")
    store.finish_run_summary(conn, second, "completed", "{}")
    latest = store.review_run(conn, second)
    assert latest is not None
    assert store.last_run_started_at(conn, "traffic-check") == latest["started_at"]
    # A different kind never confuses the lookup.
    assert store.last_run_started_at(conn, "calendar-scan") is None


def test_last_run_started_at_ignores_dry_run_rows(conn: sqlite3.Connection) -> None:
    """A dry run (#186) must never advance the live cadence clock.

    Even though the dry run is the *newest* row by id, the lookup used to gate
    the live cadence self-skip must keep returning the older live run's
    timestamp — a dry run is excluded entirely.
    """
    live = store.start_run(conn, mode="live", kind="traffic-check")
    store.finish_run_summary(conn, live, "completed", "{}")
    live_row = store.review_run(conn, live)
    assert live_row is not None

    dry = store.start_run(conn, mode="dry_run", kind="traffic-check")
    store.finish_run_summary(conn, dry, "completed", "{}")

    assert store.last_run_started_at(conn, "traffic-check") == live_row["started_at"]


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


def test_family_traffic_status_line_from_run_store(tmp_path: Path) -> None:
    """The Run-tab traffic card's status line reads last check / last alert (#164)."""
    db = tmp_path / "traffic.sqlite3"
    alert_run = _seed_family_run(db, kind="traffic-check")  # alerts == 1
    # A newer check that raised no alert: last_check advances, last_alert doesn't.
    conn = store.connect(db)
    try:
        quiet = store.start_run(conn, mode="live", kind="traffic-check")
        store.finish_run_summary(
            conn, quiet, "completed",
            json.dumps({"kind": "traffic-check", "status": "ok", "checked": [], "alerts": 0}),
        )
        alert_started = store.review_run(conn, alert_run)["started_at"]
        quiet_started = store.review_run(conn, quiet)["started_at"]
    finally:
        conn.close()

    with _client(db) as client:
        traffic = client.get("/api/family").json()["traffic"]

    assert traffic["cadence_min"] == 30  # #164 default surfaced
    assert traffic["last_check"] == quiet_started  # newest run of any outcome
    assert traffic["last_alert"] == alert_started  # newest run that alerted


def test_family_post_persists_cadence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /api/family routes cadence_min into the traffic override block (#164)."""
    import app.webapp.routers.family as family_router

    saved: dict[str, Any] = {}
    monkeypatch.setattr(
        family_router, "save_local_overrides", lambda partial: saved.update(partial) or Path("x")
    )
    db = tmp_path / "cadence.sqlite3"
    with _client(db) as client:
        assert client.post("/api/family", json={"cadence_min": 45}).status_code == 200
        assert client.post("/api/family", json={"cadence_min": 0}).status_code == 400

    assert saved["traffic"] == {"cadence_min": 45}
