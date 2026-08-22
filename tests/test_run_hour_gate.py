"""``family.run_hour`` gates the daily calendar scan (#277).

From #160 until #277 the knob was parsed, defaulted to 7, returned by
``GET /api/family`` and editable via ``POST /api/family`` — and read by no
scheduling code anywhere, while its own comment claimed it was "the local hour
the daily scan fires at/after". The real fire time was the App Launcher job's
(18:05). These pin the resolution: an unforced ``calendar-scan`` before
``run_hour`` self-skips, records **no** DB run row, spends no Calendar and no
Routes call, and cannot be mistaken by the Family tab for a completed travel
sweep. An explicit ``--force`` — which every webapp button passes — ignores the
gate entirely.

Offline throughout: the runner is stubbed and asserts it is never reached, the
config layer is redirected to a disposable file, and the clock is pinned at
``app.cli.main._local_now``. No Google, no Routes, no Telegram.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.cli import main as cli
from app.webapp import runs as webapp_runs
from src.db import store
from tests.test_unified_runs import _client

#: A wall clock pinned well before and well after the default `run_hour` of 7.
_AT_0300 = "2026-08-24T03:00:00+02:00"
_AT_0700 = "2026-08-24T07:00:00+02:00"
_AT_2200 = "2026-08-24T22:00:00+02:00"


def _isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **family: Any) -> None:
    """Point ``load_config``'s override layer at a disposable, sanitized file."""
    path = tmp_path / "local.json"
    path.write_text(json.dumps({"family": family}), encoding="utf-8")
    monkeypatch.setenv("WR_LOCAL_CONFIG_PATH", str(path))


def _at(monkeypatch: pytest.MonkeyPatch, moment: str) -> None:
    monkeypatch.setattr(cli, "_local_now", lambda: datetime.fromisoformat(moment))


def _stub_scan(monkeypatch: pytest.MonkeyPatch) -> list[datetime]:
    """Record every call to the real runner; the list is the "did it run" oracle."""
    import src.family.calendar_scan as calendar_scan

    calls: list[datetime] = []

    def fake(config: Any, *, now: datetime, dry_run: bool) -> dict[str, Any]:
        calls.append(now)
        return {
            "kind": "calendar-scan", "status": "ok", "conflicts": [],
            "missing_locations": [], "dry_run": dry_run,
        }

    monkeypatch.setattr(calendar_scan, "run_calendar_scan", fake)
    return calls


def _forbid_external_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any Calendar read or Routes call a hard failure, not a network hit.

    A skip that "spent nothing" has to be proven, not assumed: these are the two
    metered things a real scan does, and both are billed or rate-limited.
    """
    import src.family.calendar_source as calendar_source
    import src.traffic.routes_client as routes_client

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a self-skipped scan must spend no Calendar/Routes call")

    monkeypatch.setattr(calendar_source, "fetch_events_by_person", forbidden)
    monkeypatch.setattr(routes_client, "compute_route", forbidden)


def _run_dirs(runs_dir: Path, kind: str) -> list[Path]:
    return sorted((runs_dir / kind).iterdir()) if (runs_dir / kind).is_dir() else []


def _db_run_count(db: Path) -> int:
    conn = store.connect(db)
    try:
        return len(store.list_review_runs(conn, 200))
    finally:
        conn.close()


# ----------------------------------------------------------- the gate itself


def test_a_live_scan_before_run_hour_self_skips_and_spends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """03:00 against ``run_hour: 7`` — the whole point of the issue."""
    db = tmp_path / "family.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))
    _isolated_config(tmp_path, monkeypatch, enabled=True, run_hour=7)
    calls = _stub_scan(monkeypatch)
    _forbid_external_calls(monkeypatch)
    _at(monkeypatch, _AT_0300)

    assert cli.main(["calendar-scan"]) == 0
    assert calls == []
    assert _db_run_count(db) == 0  # no run row backs a skip


@pytest.mark.parametrize("moment", [_AT_0700, _AT_2200])
def test_a_live_scan_at_or_after_run_hour_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, moment: str
) -> None:
    """``run_hour`` is a floor, inclusive: 07:00 itself is not "before 7"."""
    db = tmp_path / "family.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))
    _isolated_config(tmp_path, monkeypatch, enabled=True, run_hour=7)
    calls = _stub_scan(monkeypatch)
    _at(monkeypatch, moment)

    assert cli.main(["calendar-scan"]) == 0
    assert len(calls) == 1
    assert _db_run_count(db) == 1


def test_run_hour_zero_never_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The opt-out: a household that wants the job's own schedule honoured."""
    monkeypatch.setenv("WR_DB_PATH", str(tmp_path / "family.sqlite3"))
    _isolated_config(tmp_path, monkeypatch, enabled=True, run_hour=0)
    calls = _stub_scan(monkeypatch)
    _at(monkeypatch, _AT_0300)

    assert cli.main(["calendar-scan"]) == 0
    assert len(calls) == 1


def test_the_traffic_check_is_untouched_by_the_hour_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``run_hour`` is the calendar scan's knob; the sibling verb keeps cadence."""
    monkeypatch.setenv("WR_DB_PATH", str(tmp_path / "family.sqlite3"))
    _isolated_config(tmp_path, monkeypatch, enabled=True, run_hour=23)
    import src.family.traffic_check as traffic_check

    calls: list[datetime] = []

    def fake(config: Any, *, now: datetime, dry_run: bool) -> dict[str, Any]:
        calls.append(now)
        return {"kind": "traffic-check", "status": "ok", "checked": [], "alerts": 0}

    monkeypatch.setattr(traffic_check, "run_traffic_check", fake)
    _at(monkeypatch, _AT_0300)

    assert cli.main(["traffic-check"]) == 0
    assert len(calls) == 1


# ------------------------------------------- an explicit human request wins


def test_force_runs_before_run_hour(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator pressing "Run sweep" at 22:00 has asked for a run."""
    db = tmp_path / "family.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))
    _isolated_config(tmp_path, monkeypatch, enabled=True, run_hour=23)
    calls = _stub_scan(monkeypatch)
    _at(monkeypatch, _AT_2200)

    assert cli.main(["calendar-scan", "--force"]) == 0
    assert len(calls) == 1
    assert _db_run_count(db) == 1


def test_a_dry_run_alone_is_not_an_exemption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mode is not the permission — ``--force`` is (deliberately unlike #186).

    ``traffic-check`` exempts ``--dry-run`` from its cadence because that was
    the only manual path it had. Here the manual path is explicit, so the
    exemption is explicit too and a scheduled dry fire is gated like any other.
    """
    monkeypatch.setenv("WR_DB_PATH", str(tmp_path / "family.sqlite3"))
    _isolated_config(tmp_path, monkeypatch, enabled=True, run_hour=7)
    calls = _stub_scan(monkeypatch)
    _forbid_external_calls(monkeypatch)
    _at(monkeypatch, _AT_0300)

    assert cli.main(["calendar-scan", "--dry-run"]) == 0
    assert calls == []

    assert cli.main(["calendar-scan", "--dry-run", "--force"]) == 0
    assert len(calls) == 1


def test_every_webapp_launched_scan_is_forced() -> None:
    """The webapp schedules nothing — each of its runs is a button press (#276)."""
    from app.webapp.routers.execution import _compose_argv

    assert _compose_argv("calendar-scan", {"mode": "live"}) == ["calendar-scan", "--force"]
    assert _compose_argv("calendar-scan", {"mode": "dry_run"}) == [
        "calendar-scan", "--dry-run", "--force",
    ]
    # The sibling verb keeps its own argv — --force does not exist there.
    assert _compose_argv("traffic-check", {"mode": "live"}) == ["traffic-check"]


# ------------------------------------------------- the skip is its own state


def test_the_skip_is_recorded_as_skipped_not_as_success_or_failure(
    isolated_runs_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors the cadence skip's record shape — auditable, and not a run."""
    monkeypatch.setenv("WR_DB_PATH", str(tmp_path / "family.sqlite3"))
    _isolated_config(tmp_path, monkeypatch, enabled=True, run_hour=7)
    _stub_scan(monkeypatch)
    _at(monkeypatch, _AT_0300)

    assert cli.main(["calendar-scan"]) == 0

    (run_dir,) = _run_dirs(isolated_runs_dir, "calendar-scan")
    record = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert record["status"] == "completed"          # a skip is not a failure
    assert record["result"]["status"] == "skipped"  # ...and not a success either
    assert "run_hour" in record["result"]["reason"]
    assert "--force" in record["result"]["reason"]  # says how to override
    assert "error" not in record["result"]
    assert "db_run_id" not in record                # no DB row backs a skip
    assert "skipped" in (run_dir / "output.log").read_text(encoding="utf-8")


def test_the_skip_is_hidden_behind_the_count_like_a_cadence_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same treatment as #234's: out of Recent runs, still counted and fetchable."""
    monkeypatch.setattr(webapp_runs, "RUNS_DIR", tmp_path / "runs")
    run_dir = webapp_runs.new_run_dir("calendar-scan", "20260824T030000")
    webapp_runs.write_run_json(
        run_dir,
        kind="calendar-scan",
        status="completed",
        started_at="2026-08-24T03:00:00+02:00",
        result={
            "kind": "calendar-scan", "status": "skipped",
            "reason": "before family.run_hour=07:00 (local hour 03)",
        },
    )

    with _client(tmp_path / "exec.sqlite3") as client:
        default = client.get("/api/execution/runs").json()
        revealed = client.get(
            "/api/execution/runs", params={"include_skipped": "true"}
        ).json()

    assert default["runs"] == []
    assert default["skipped_count"] == 1
    assert len(revealed["runs"]) == 1
    assert revealed["runs"][0]["result"]["status"] == "skipped"


def test_a_skipped_scan_leaves_the_last_travel_sweep_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Family tab must still show the genuine sweep — not the skip, not nothing.

    ``_last_travel_sweep`` walks ``review_runs`` for the newest ``calendar-scan``
    payload carrying a ``travel_blocks`` section. A skip that recorded a row
    would either be read as a completed sweep or, carrying no section, blank the
    card back to "never run".
    """
    db = tmp_path / "family.sqlite3"
    monkeypatch.setenv("WR_DB_PATH", str(db))
    _isolated_config(tmp_path, monkeypatch, enabled=True, run_hour=7)

    conn = store.connect(db)
    try:
        genuine = store.start_run(conn, mode="live", kind="calendar-scan")
        store.finish_run_summary(
            conn, genuine, "completed",
            json.dumps({
                "kind": "calendar-scan", "status": "ok",
                "travel_blocks": {"status": "ok", "dry_run": True, "routes_calls": 3},
            }),
        )
    finally:
        conn.close()

    _stub_scan(monkeypatch)
    _forbid_external_calls(monkeypatch)
    _at(monkeypatch, _AT_0300)
    assert cli.main(["calendar-scan"]) == 0

    with _client(db) as client:
        payload = client.get("/api/family").json()

    sweep = payload["travel_blocks"]["last_sweep"]
    assert sweep is not None
    assert sweep["run_id"] == f"db-{genuine}"
    assert sweep["status"] == "ok"
    assert sweep["routes_calls"] == 3
    # And the skip left no phantom behind in the tab's own run list: the only
    # calendar-scan the Family tab knows about is the sweep that really ran.
    assert [run["run_id"] for run in payload["runs"]] == [f"db-{genuine}"]


def test_a_skip_row_would_not_be_read_as_a_sweep_either(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Belt and braces on ``_last_travel_sweep``'s section check.

    The gate records no DB row, so this state is unreachable today — but the
    lookup's "newest ``calendar-scan``" walk is one edit away from treating any
    such row as the sweep, and the card would then read "never run" over a
    perfectly good sweep from an hour earlier.
    """
    db = tmp_path / "family.sqlite3"
    _isolated_config(tmp_path, monkeypatch, enabled=True)

    conn = store.connect(db)
    try:
        genuine = store.start_run(conn, mode="live", kind="calendar-scan")
        store.finish_run_summary(
            conn, genuine, "completed",
            json.dumps({
                "kind": "calendar-scan", "status": "ok",
                "travel_blocks": {"status": "ok", "dry_run": True, "routes_calls": 3},
            }),
        )
        newer = store.start_run(conn, mode="live", kind="calendar-scan")
        store.finish_run_summary(
            conn, newer, "completed",
            json.dumps({"kind": "calendar-scan", "status": "skipped",
                        "reason": "before family.run_hour=07:00 (local hour 03)"}),
        )
    finally:
        conn.close()

    with _client(db) as client:
        sweep = client.get("/api/family").json()["travel_blocks"]["last_sweep"]

    assert sweep is not None
    assert sweep["run_id"] == f"db-{genuine}"
    assert sweep["routes_calls"] == 3
