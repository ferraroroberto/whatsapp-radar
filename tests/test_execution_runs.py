"""Run-record infra (app/webapp/runs.py) + the result sentinel (src/runresult.py).

Covers the pure helpers (write/read/tail/list, sentinel round-trip) plus one
real end-to-end spawn: launch ``launcher.py scan --dry-run`` against a fixture
DB, poll the run record to completion, and assert the funnel comes back, nothing
was delivered, and no cursor advanced.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.webapp import runs
from src.connector.fixture import FixtureConnector
from src.db import store
from src.db.sync import resync
from src.runresult import RESULT_SENTINEL, format_result, parse_result

# --- result sentinel -------------------------------------------------------

def test_result_sentinel_round_trip() -> None:
    payload = {"kind": "scan", "ok": True, "funnel": {"actionable": 2}}
    line = format_result(payload)
    assert line.startswith(RESULT_SENTINEL)
    assert parse_result("noise\n" + line + "\nmore noise") == payload


def test_parse_result_absent_and_last_wins() -> None:
    assert parse_result("just some output\nno sentinel here") is None
    text = format_result({"n": 1}) + "\n" + format_result({"n": 2})
    assert parse_result(text) == {"n": 2}


# --- run-record helpers ----------------------------------------------------

def test_write_read_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    run_dir = runs._new_run_dir("scan", runs.new_run_id())
    runs.write_run_json(run_dir, kind="scan", status="running")
    runs.write_run_json(run_dir, status="completed", exit_code=0)
    record = runs.read_run(run_dir)
    assert record["kind"] == "scan"  # merged, not clobbered
    assert record["status"] == "completed"
    assert record["exit_code"] == 0


def test_output_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    run_dir = runs._new_run_dir("resync", runs.new_run_id())
    (run_dir / "output.log").write_bytes(b"line one\nline two\nline three\n")
    tail = runs.read_output_tail(run_dir)
    assert "line three" in tail


def test_list_runs_newest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    d1 = runs._new_run_dir("scan", "20260101T000000")
    runs.write_run_json(d1, kind="scan", status="completed", started_at="2026-01-01T00:00:00")
    d2 = runs._new_run_dir("resync", "20260102T000000")
    runs.write_run_json(d2, kind="resync", status="completed", started_at="2026-01-02T00:00:00")
    listed = runs.list_runs()
    assert [r["kind"] for r in listed] == ["resync", "scan"]


# --- spawn → poll → funnel (end to end) ------------------------------------

def _poll(kind: str, run_id: str, timeout: float = 90.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = runs.get_run(kind, run_id)
        assert record is not None
        if record.get("status") in ("completed", "failed"):
            return record
        time.sleep(0.25)
    raise AssertionError("run did not finish in time")


def test_dry_run_scan_spawn_reports_funnel_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setenv("WR_CONNECTOR", "fixture")
    monkeypatch.setenv("WR_CLASSIFIER", "stub")
    monkeypatch.setenv("WR_NOTIFIER", "none")

    db = tmp_path / "exec.sqlite3"
    conn = store.connect(db)
    resync(conn, FixtureConnector())
    chat_id = store.chat_id_for_source(conn, "chat-class-4a")
    assert chat_id is not None
    store.set_chat_status(conn, chat_id, "monitored")
    store.baseline_cursor(conn, chat_id)
    cursor_before = conn.execute(
        "SELECT last_processed_message_id FROM chat_review_state WHERE chat_id = ?", (chat_id,)
    ).fetchone()["last_processed_message_id"]
    conn.close()

    started = runs.start_run("scan", ["scan", "--dry-run"], env_overrides={"WR_DB_PATH": str(db)})
    record = _poll(started["kind"], started["run_id"])

    assert record["status"] == "completed"
    result = record.get("result")
    assert result is not None
    assert result["kind"] == "scan"
    assert result["notification_status"] == "dry_run"
    assert "funnel" in result
    assert "▶ scan [dry_run] starting" in record["output_tail"]

    # No delivery, and the dry-run advanced no cursor.
    after = store.connect(db)
    try:
        assert after.execute("SELECT COUNT(*) AS n FROM notifications").fetchone()["n"] == 0
        cursor_after = after.execute(
            "SELECT last_processed_message_id FROM chat_review_state WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()["last_processed_message_id"]
        assert cursor_after == cursor_before
    finally:
        after.close()
