"""CLI-side run-output capture (#233).

A run fired from a terminal or an App Launcher Job used to leave the Execution
tab's viewer showing a "no captured output" placeholder — the outcome was
recorded, the reason was not. :mod:`app.cli.runlog` tees the CLI's own output
into the same filesystem run record the webapp writes, so these assert both the
capture itself and the end-to-end result: a scheduled run's log reaching the API
the phone reads.

Offline throughout: the family runner is stubbed, no network, no Google.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from app.cli import main as cli
from app.cli import runlog
from app.webapp import runs as webapp_runs
from app.webapp.server import create_app
from src.webapp_config import WebappConfig

LOOPBACK = ("127.0.0.1", 5555)

#: Mirrors the real runner's payload shape (``src/family/traffic_check.py``) —
#: ``kind`` included, because the viewer switches its funnel tiles on it.
_OK_PAYLOAD: dict[str, Any] = {
    "kind": "traffic-check",
    "status": "ok",
    "checked": [],
    "alerts": 0,
}


def _client(db: Path) -> TestClient:
    app = create_app()
    app.state.webapp_config = WebappConfig(auth_token="")
    app.state.db_path = db
    return TestClient(app, client=LOOPBACK)


def _stub_traffic_check(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    import src.family.traffic_check as traffic_check

    def fake_runner(config: Any, *, now: Any, dry_run: bool) -> dict[str, Any]:
        print("probing route 1")  # a progress line only the log can show
        return dict(payload, dry_run=dry_run)

    monkeypatch.setattr(traffic_check, "run_traffic_check", fake_runner)


def _only_run_dir(runs_dir: Path, kind: str) -> Path:
    children = sorted((runs_dir / kind).iterdir())
    assert len(children) == 1, f"expected one {kind} run dir, got {children}"
    return children[0]


# --- verb gating -------------------------------------------------------------

def test_launchable_verbs_match_the_cli_parser() -> None:
    """Every captured verb must be a real command, or capture silently misses it."""
    parser = cli.build_parser()
    subparsers = next(
        action.choices  # type: ignore[attr-defined]
        for action in parser._actions
        if hasattr(action, "choices") and action.choices
    )
    assert runlog.LAUNCHABLE_VERBS <= set(subparsers)


def test_review_verb_files_under_the_webapp_kind() -> None:
    # The webapp calls this action "process"; one execution must not file under
    # two kinds or the viewer's per-kind lookup misses it.
    assert runlog.run_kind("review") == "process"
    assert runlog.run_kind("traffic-check") == "traffic-check"


def test_non_launchable_verb_writes_no_run_record(
    isolated_runs_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WR_DB_PATH", str(tmp_path / "chats.sqlite3"))
    assert cli.main(["chats"]) == 0
    assert not isolated_runs_dir.exists()


# --- capture ----------------------------------------------------------------

def test_cli_run_captures_output_and_stamps_the_db_run_id(
    isolated_runs_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WR_DB_PATH", str(tmp_path / "family.sqlite3"))
    _stub_traffic_check(monkeypatch, {**_OK_PAYLOAD, "checked": [{"person": "dad"}]})

    assert cli.main(["traffic-check", "--dry-run"]) == 0

    run_dir = _only_run_dir(isolated_runs_dir, "traffic-check")
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["origin"] == "cli"
    assert record["status"] == "completed"
    assert record["exit_code"] == 0
    assert record["argv"] == ["traffic-check", "--dry-run"]
    # The DB run id is the merge key that unifies this with its DB row (#163).
    assert isinstance(record["db_run_id"], int)
    assert record["result"]["status"] == "ok"

    log = (run_dir / "output.log").read_text(encoding="utf-8")
    assert "probing route 1" in log      # the progress the placeholder used to hide
    assert "__WR_RESULT__" in log        # sentinel intact for the watcher/parser


def test_webapp_captured_marker_suppresses_a_second_record(
    isolated_runs_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A webapp-spawned child must not open a record for a process already captured."""
    monkeypatch.setenv("WR_DB_PATH", str(tmp_path / "family.sqlite3"))
    monkeypatch.setenv(webapp_runs.CAPTURED_ENV_VAR, "1")
    _stub_traffic_check(monkeypatch, _OK_PAYLOAD)

    assert cli.main(["traffic-check", "--dry-run"]) == 0
    assert not isolated_runs_dir.exists()


def test_start_run_sets_the_captured_marker_on_its_child() -> None:
    # The suppression above is only reachable if the webapp actually sets it.
    import inspect

    source = inspect.getsource(webapp_runs.start_run)
    assert "CAPTURED_ENV_VAR" in source


def test_failed_run_records_the_failure_and_its_reason(
    isolated_runs_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("WR_DB_PATH", str(tmp_path / "family.sqlite3"))
    import src.family.traffic_check as traffic_check

    def boom(config: Any, *, now: Any, dry_run: bool) -> dict[str, Any]:
        raise RuntimeError("routes api down")

    monkeypatch.setattr(traffic_check, "run_traffic_check", boom)

    assert cli.main(["traffic-check"]) == 1

    run_dir = _only_run_dir(isolated_runs_dir, "traffic-check")
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert record["exit_code"] == 1
    assert "routes api down" in (run_dir / "output.log").read_text(encoding="utf-8")


def test_escaping_exception_lands_in_the_log_before_streams_restore(
    isolated_runs_dir: Path
) -> None:
    """An uncaught error must leave its traceback *in the record*, not just on stderr."""

    def explode() -> int:
        raise ValueError("unexpected boom")

    with pytest.raises(ValueError, match="unexpected boom"):
        runlog.run_captured("scan", ["scan"], explode)

    run_dir = _only_run_dir(isolated_runs_dir, "scan")
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "failed"
    assert "exit_code" not in record  # no code to report — not guessed at
    log = (run_dir / "output.log").read_text(encoding="utf-8")
    assert "ValueError: unexpected boom" in log
    assert "Traceback" in log


def test_capture_survives_an_unwritable_runs_dir(
    monkeypatch: pytest.MonkeyPatch, isolated_runs_dir: Path
) -> None:
    """Capture is observability — it must never take the command down with it."""

    def refuse(kind: str, run_id: str) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(webapp_runs, "new_run_dir", refuse)
    assert runlog.run_captured("scan", ["scan"], lambda: 0) == 0


# --- the cadence self-skip (a visible, distinctly-labelled non-run) ----------

def test_self_skipped_run_is_visible_and_distinct_from_an_error(
    isolated_runs_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A skipped fire is exactly the "why did nothing happen" answer #233 is for.

    It records no DB row (the skip returns before ``store.start_run``), so it
    stands alone in the runs list — and must read as *skipped*, never as failed
    or errored.
    """
    db = tmp_path / "family.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))

    def never_called(config: Any, *, now: Any, dry_run: bool) -> dict[str, Any]:
        raise AssertionError("the runner must not run when the cadence self-skips")

    import src.family.traffic_check as traffic_check

    monkeypatch.setattr(traffic_check, "run_traffic_check", never_called)
    monkeypatch.setattr(
        cli, "_traffic_cadence_skip_reason", lambda conn, config: "cadence 30min not elapsed"
    )

    assert cli.main(["traffic-check"]) == 0

    run_dir = _only_run_dir(isolated_runs_dir, "traffic-check")
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "completed"        # a skip is not a failure
    assert record["result"]["status"] == "skipped"
    assert "cadence" in record["result"]["reason"]
    assert "error" not in record["result"]       # distinct from the error payload
    assert "db_run_id" not in record             # no DB row backs a skip
    assert "skipped" in (run_dir / "output.log").read_text(encoding="utf-8")


# --- end to end: the API the phone reads ------------------------------------

def test_cli_launched_run_surfaces_output_in_the_api(
    isolated_runs_dir: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole point: a Jobs-style run shows its log, merged into one entry."""
    db = tmp_path / "family.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))
    _stub_traffic_check(monkeypatch, _OK_PAYLOAD)

    assert cli.main(["traffic-check", "--dry-run"]) == 0

    with _client(db) as client:
        body = client.get("/api/execution/runs").json()
        kind, run_id = body["runs"][0]["kind"], body["runs"][0]["run_id"]
        detail = client.get(f"/api/execution/runs/{kind}/{run_id}").json()

    # One entry, not two: the filesystem record merged with its DB row.
    assert len(body["runs"]) == 1
    assert body["runs"][0]["origin"] == "cli"
    assert body["runs"][0]["mode"] == "dry_run"
    assert "probing route 1" in detail["run"]["output_tail"]
    assert "no captured output" not in detail["run"]["output_tail"]


def test_runs_list_is_bounded_per_kind(
    isolated_runs_dir: Path
) -> None:
    """The list endpoint reads at most ``limit`` records per kind.

    Without a bound this scans every directory on every Execution-tab poll, and
    since #233 the scheduled ``traffic-check`` alone contributes ~288 a day (it
    fires every 5 minutes against a 30-minute cadence). A read bound, not a
    cleanup — ``list_runs`` itself never deletes a run (the retention cap lives
    in ``webapp_runs.prune_runs``, #234).
    """
    for i in range(12):
        run_dir = webapp_runs.new_run_dir("scan", f"202601{i + 1:02d}T000000")
        webapp_runs.write_run_json(
            run_dir, kind="scan", run_id=run_dir.name, status="completed",
            started_at=f"2026-01-{i + 1:02d}T00:00:00+00:00",
        )

    listed = webapp_runs.list_runs(limit=3)
    assert [r["run_id"] for r in listed] == [
        "20260112T000000", "20260111T000000", "20260110T000000",
    ]
    # All 12 still on disk — bounded reading, not pruning.
    assert len(list((isolated_runs_dir / "scan").iterdir())) == 12


def test_conftest_isolation_keeps_the_real_runs_dir_untouched(
    isolated_runs_dir: Path
) -> None:
    """Guard on the guard: RUNS_DIR must be redirected for every test."""
    assert webapp_runs.RUNS_DIR == isolated_runs_dir
    assert "webapp-runs" in str(webapp_runs.RUNS_DIR)


def test_sqlite_row_factory_unaffected(conn: sqlite3.Connection) -> None:
    # Sanity: the capture wrapper does not disturb the store's connection setup.
    assert conn.row_factory is sqlite3.Row
