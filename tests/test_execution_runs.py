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
from src.runresult import RESULT_SENTINEL, format_result, parse_result, strip_result_line

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


def test_strip_result_line_removes_sentinel_and_notes_it() -> None:
    text = "hello\n" + format_result({"a": 1}) + "\nbye"
    stripped = strip_result_line(text)
    assert RESULT_SENTINEL not in stripped
    assert "hello" in stripped
    assert "bye" in stripped
    assert "withheld" in stripped


def test_strip_result_line_passthrough_when_no_sentinel() -> None:
    text = "just output\nno sentinel here"
    assert strip_result_line(text) == text


# --- #292: the Execution tab's output panel must not paint the result -------
# payload (calendar ids, street addresses) into the DOM verbatim -------------

def test_get_run_output_tail_withholds_the_result_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sentinel line the webapp parses is also the whole result payload —
    calendar ids and street addresses included, for a calendar-scan run. It
    must never reach a run's *displayed* output, even though the log file
    itself still carries it intact for `parse_result` to recover.

    Planted per-field sentinels, not an email-shaped regex: a street address
    is exactly the kind of value a `"@" not in text` check would miss.
    """
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    run_dir = runs.new_run_dir("calendar-scan", runs.new_run_id())
    calendar_id = "SENTINELCALENDARID@leak.invalid"
    origin = "SENTINELORIGINSTREET 1"
    destination = "SENTINELDESTINATIONSTREET 2"
    payload = {
        "kind": "calendar-scan",
        "status": "ok",
        "travel_blocks": {
            "adds": [
                {"calendar_id": calendar_id, "origin": origin, "destination": destination},
            ],
        },
    }
    log_text = "▶ calendar-scan [live] starting\n" + format_result(payload) + "\n"
    (run_dir / "output.log").write_bytes(log_text.encode("utf-8"))
    runs.write_run_json(run_dir, kind="calendar-scan", status="completed")

    record = runs.get_run("calendar-scan", run_dir.name)

    assert record is not None
    for sentinel in (calendar_id, origin, destination):
        assert sentinel not in record["output_tail"]
    assert "▶ calendar-scan [live] starting" in record["output_tail"]
    assert "withheld" in record["output_tail"]

    # The machine-readable side is unaffected: parse_result still recovers the
    # structured result from the untouched log file.
    assert parse_result(runs.read_output_tail(run_dir)) == payload


# --- run-record helpers ----------------------------------------------------

def test_write_read_merge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    run_dir = runs.new_run_dir("scan", runs.new_run_id())
    runs.write_run_json(run_dir, kind="scan", status="running")
    runs.write_run_json(run_dir, status="completed", exit_code=0)
    record = runs.read_run(run_dir)
    assert record["kind"] == "scan"  # merged, not clobbered
    assert record["status"] == "completed"
    assert record["exit_code"] == 0


def test_output_tail(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    run_dir = runs.new_run_dir("resync", runs.new_run_id())
    (run_dir / "output.log").write_bytes(b"line one\nline two\nline three\n")
    tail = runs.read_output_tail(run_dir)
    assert "line three" in tail


def test_list_runs_newest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    d1 = runs.new_run_dir("scan", "20260101T000000")
    runs.write_run_json(d1, kind="scan", status="completed", started_at="2026-01-01T00:00:00")
    d2 = runs.new_run_dir("resync", "20260102T000000")
    runs.write_run_json(d2, kind="resync", status="completed", started_at="2026-01-02T00:00:00")
    listed = runs.list_runs()
    assert [r["kind"] for r in listed] == ["resync", "scan"]


# --- retention (#234) -------------------------------------------------------

def _seed_completed(kind_dir_kind: str, run_id: str, started_at: str) -> None:
    run_dir = runs.new_run_dir(kind_dir_kind, run_id)
    runs.write_run_json(
        run_dir, kind=kind_dir_kind, status="completed", started_at=started_at
    )


def test_prune_runs_caps_each_kind_to_the_retention_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runs, "_RETENTION_PER_KIND", 3)
    for i in range(5):
        run_id = f"2026010{i + 1}T000000"
        started = f"2026-01-0{i + 1}T00:00:00+00:00"
        _seed_completed("traffic-check", run_id, started)

    runs.prune_runs()

    remaining = sorted(p.name for p in (tmp_path / "runs" / "traffic-check").iterdir())
    assert remaining == ["20260103T000000", "20260104T000000", "20260105T000000"]


def test_prune_runs_never_deletes_the_active_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runs, "_RETENTION_PER_KIND", 1)
    monkeypatch.setattr(runs, "active_run", lambda: {"kind": "scan", "run_id": "20260101T000000"})
    _seed_completed("scan", "20260101T000000", "2026-01-01T00:00:00+00:00")
    _seed_completed("scan", "20260102T000000", "2026-01-02T00:00:00+00:00")

    runs.prune_runs()

    remaining = {p.name for p in (tmp_path / "runs" / "scan").iterdir()}
    assert "20260101T000000" in remaining  # the active one, even though it's older


def test_prune_runs_keeps_a_recent_running_record_past_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A genuinely in-flight CLI/Jobs run (no webapp handle) must survive."""
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runs, "_RETENTION_PER_KIND", 0)
    monkeypatch.setattr(runs, "active_run", lambda: None)
    run_dir = runs.new_run_dir("traffic-check", "20260101T000000")
    runs.write_run_json(
        run_dir, kind="traffic-check", status="running", started_at=runs.now_iso()
    )

    runs.prune_runs()

    assert run_dir.is_dir()


def test_prune_runs_removes_a_stale_running_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed CLI run must not stay 'running' — and immune to pruning — forever."""
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runs, "_RETENTION_PER_KIND", 0)
    monkeypatch.setattr(runs, "active_run", lambda: None)
    run_dir = runs.new_run_dir("traffic-check", "20260101T000000")
    runs.write_run_json(
        run_dir, kind="traffic-check", status="running",
        started_at="2020-01-01T00:00:00+00:00",  # ancient — the process is long dead
    )

    runs.prune_runs()

    assert not run_dir.exists()


def test_prune_runs_tolerates_a_locked_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An open output.log (Windows can't unlink it) must not crash the sweep."""
    monkeypatch.setattr(runs, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(runs, "_RETENTION_PER_KIND", 0)
    monkeypatch.setattr(runs, "active_run", lambda: None)
    _seed_completed("scan", "20260101T000000", "2026-01-01T00:00:00+00:00")
    log_path = tmp_path / "runs" / "scan" / "20260101T000000" / "output.log"
    log_path.write_bytes(b"")
    handle = log_path.open("rb")
    try:
        with caplog.at_level("WARNING"):
            runs.prune_runs()  # must not raise
        assert (tmp_path / "runs" / "scan" / "20260101T000000").is_dir()
        assert "locked" in caplog.text.lower() or "could not prune" in caplog.text.lower()
    finally:
        handle.close()


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
