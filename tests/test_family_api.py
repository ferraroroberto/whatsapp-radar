"""Family rules command center: /api/family read + POST validation (#167, #268).

Every rule in the Family tab's "Rules in force" card is editable and server-
validated before it lands in config/local.json: times parse as HH:MM, an
on-duty pattern names exactly the 7 weekdays, and a childcare window's
optional end must come after its start (non-inverted). The travel-block card
(#268) adds its own knobs on the same endpoint, plus the read side that
reports per-calendar write capability as three states and the last sweep's
counts. Offline throughout — `save_local_overrides` is monkeypatched (or
`WR_LOCAL_CONFIG_PATH` redirected) so nothing touches the real, gitignored
config/local.json.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import app.webapp.routers.family as family_router
from src.db import store
from tests.test_unified_runs import _client


def _patched_save(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    saved: dict[str, Any] = {}
    monkeypatch.setattr(
        family_router, "save_local_overrides", lambda partial: saved.update(partial) or Path("x")
    )
    return saved


# --------------------------------------------------------------- kids_home_time


def test_kids_home_time_valid_saves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    saved = _patched_save(monkeypatch)
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"kids_home_time": "17:45"})
    assert res.status_code == 200
    assert saved["family"]["kids_home_time"] == "17:45"


@pytest.mark.parametrize("bad", ["25:00", "17:75", "not-a-time", "17", ""])
def test_kids_home_time_invalid_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    _patched_save(monkeypatch)
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"kids_home_time": bad})
    assert res.status_code == 400
    assert "kids_home_time" in res.json()["detail"]


# -------------------------------------------------- skip_leave_now_for_train (#227)


@pytest.mark.parametrize("value", [True, False])
def test_skip_leave_now_for_train_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: bool
) -> None:
    saved = _patched_save(monkeypatch)
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"skip_leave_now_for_train": value})
        assert res.status_code == 200
        assert saved["traffic"]["skip_leave_now_for_train"] is value
        # The GET side exposes both the toggle and the keyword list it matches on.
        payload = client.get("/api/family").json()
    assert "skip_leave_now_for_train" in payload["traffic"]
    assert payload["traffic"]["train_keywords"] == ["tren", "train"]


# --------------------------------------------------------- responsible_by_weekday


_FULL_WEEK = {
    "Mon": "roberto", "Tue": "ana", "Wed": "", "Thu": "roberto",
    "Fri": "ana", "Sat": "", "Sun": "",
}


def test_responsible_by_weekday_complete_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = _patched_save(monkeypatch)
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"responsible_by_weekday": _FULL_WEEK})
    assert res.status_code == 200
    # Persisted with canonical lowercase 3-letter keys — matches the existing
    # config/local.json convention so a deep-merge never orphans a mismatched key.
    assert saved["family"]["responsible_by_weekday"] == {
        "mon": "roberto", "tue": "ana", "wed": "", "thu": "roberto",
        "fri": "ana", "sat": "", "sun": "",
    }


def test_responsible_by_weekday_missing_day_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patched_save(monkeypatch)
    partial = dict(_FULL_WEEK)
    del partial["Sun"]
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"responsible_by_weekday": partial})
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "Sun" in detail


def test_responsible_by_weekday_unknown_key_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patched_save(monkeypatch)
    bad = dict(_FULL_WEEK)
    bad["Someday"] = "roberto"
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"responsible_by_weekday": bad})
    assert res.status_code == 400


# --------------------------------------------------------------- childcare_windows


def test_childcare_window_valid_range_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = _patched_save(monkeypatch)
    windows = [{"label": "swim", "days": ["Mon", "Wed"], "time": "16:45", "end_time": "17:30"}]
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"childcare_windows": windows})
    assert res.status_code == 200
    assert saved["family"]["childcare_windows"] == [
        {"label": "swim", "weekdays": ["mon", "wed"], "time": "16:45", "end_time": "17:30"}
    ]


def test_childcare_window_point_deadline_still_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No end_time at all keeps working — the legacy single-deadline shape."""
    saved = _patched_save(monkeypatch)
    windows = [{"label": "pickup", "days": ["Fri"], "time": "15:00"}]
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"childcare_windows": windows})
    assert res.status_code == 200
    assert saved["family"]["childcare_windows"][0]["end_time"] == ""


def test_childcare_window_inverted_end_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patched_save(monkeypatch)
    windows = [{"label": "swim", "days": ["Mon"], "time": "17:30", "end_time": "16:45"}]
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"childcare_windows": windows})
    assert res.status_code == 400
    assert "non-inverted" in res.json()["detail"]


def test_childcare_window_equal_end_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A zero-length range is also inverted — end must be strictly after start."""
    _patched_save(monkeypatch)
    windows = [{"label": "swim", "days": ["Mon"], "time": "16:45", "end_time": "16:45"}]
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"childcare_windows": windows})
    assert res.status_code == 400


def test_childcare_window_missing_label_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patched_save(monkeypatch)
    windows = [{"label": "  ", "days": ["Mon"], "time": "16:45"}]
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"childcare_windows": windows})
    assert res.status_code == 400


def test_childcare_window_empty_days_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patched_save(monkeypatch)
    windows = [{"label": "swim", "days": [], "time": "16:45"}]
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"childcare_windows": windows})
    assert res.status_code == 400


def test_childcare_window_bad_weekday_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patched_save(monkeypatch)
    windows = [{"label": "swim", "days": ["Someday"], "time": "16:45"}]
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"childcare_windows": windows})
    assert res.status_code == 400


def test_childcare_window_bad_time_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patched_save(monkeypatch)
    windows = [{"label": "swim", "days": ["Mon"], "time": "not-a-time"}]
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"childcare_windows": windows})
    assert res.status_code == 400


def test_family_api_reports_the_effective_dedup_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Advertising only the configured value would name a window not in force (#252)."""
    with _client(tmp_path / "x.sqlite3") as client:
        traffic = client.get("/api/family").json()["traffic"]
    assert traffic["effective_dedup_window_min"] >= traffic["dedup_window_min"]


# ------------------------------------------------- ask_missing_locations (#253)


@pytest.mark.parametrize("value", [True, False])
def test_ask_missing_locations_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: bool
) -> None:
    saved = _patched_save(monkeypatch)
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"ask_missing_locations": value})
        assert res.status_code == 200
        assert saved["family"]["ask_missing_locations"] is value
        payload = client.get("/api/family").json()
    assert payload["family"]["ask_missing_locations"] is True  # committed default


# ---------------------------------------------------- travel blocks (#268)
#
# The Family tab's travel-block card is a pure *reporting* surface over the
# newest calendar-scan run: rendering it must never re-plan, re-price or write
# anything. These tests seed run payloads by hand and drive the API offline —
# no Google, no Routes, no calendar. Calendar ids are `example.com` placeholders
# and the config layer is redirected to a disposable file, so nothing here ever
# reads or writes the developer's real household config.


def _isolated_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    family: dict[str, Any] | None = None,
    calendar: dict[str, Any] | None = None,
) -> None:
    """Point ``load_config``'s override layer at a disposable, sanitized file."""
    path = tmp_path / "local.json"
    path.write_text(
        json.dumps({"family": family or {}, "calendar": calendar or {}}), encoding="utf-8"
    )
    monkeypatch.setenv("WR_LOCAL_CONFIG_PATH", str(path))


_ACCOUNTS = [
    {"calendar_id": "parent-a@example.com", "person": "parent-a", "label": "Parent A"},
    {"calendar_id": "parent-b@example.com", "person": "parent-b", "label": "Parent B"},
    {"calendar_id": "parent-c@example.com", "person": "parent-c", "label": "Parent C"},
]


def _seed_calendar_scan(db: Path, travel_blocks: dict[str, Any] | None) -> int:
    """One completed calendar-scan run row carrying (or lacking) a sweep section."""
    conn = store.connect(db)
    try:
        run_id = store.start_run(conn, mode="live", kind="calendar-scan")
        payload: dict[str, Any] = {"kind": "calendar-scan", "status": "ok", "conflicts": []}
        if travel_blocks is not None:
            payload["travel_blocks"] = travel_blocks
        store.finish_run_summary(conn, run_id, "completed", json.dumps(payload))
        return run_id
    finally:
        conn.close()


def _ok_sweep(**overrides: Any) -> dict[str, Any]:
    sweep: dict[str, Any] = {
        "status": "ok",
        "dry_run": True,
        "routes_calls": 3,
        "counts": {
            "desired": 4, "adds": 2, "deletes": 1,
            "keeps": 1, "protected": 0, "failures": 1,
        },
    }
    sweep.update(overrides)
    return sweep


def test_travel_blocks_reports_the_five_config_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The committed defaults: off, dry-run, and the three tuning knobs."""
    _isolated_config(tmp_path, monkeypatch)
    with _client(tmp_path / "x.sqlite3") as client:
        tb = client.get("/api/family").json()["travel_blocks"]
    assert tb["enabled"] is False
    assert tb["dry_run"] is True
    assert tb["horizon_days"] == 2
    assert tb["min_home_dwell_min"] == 45
    assert tb["title_template"]
    assert tb["last_sweep"] is None
    assert tb["duplicate_calendars"] == []


def test_duplicate_calendars_are_reported_by_label_never_by_raw_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#273: a duplicate `calendar_id` collapses at config-parse time and is
    surfaced on this same reporting endpoint — by label, never by the raw
    calendar id, so this stays safe to render on the Family tab.
    """
    _isolated_config(
        tmp_path,
        monkeypatch,
        calendar={
            "accounts": [
                {"calendar_id": "shared@example.com", "person": "parent-a", "label": "Parent A"},
                {
                    "calendar_id": "shared@example.com",
                    "person": "parent-b",
                    "label": "Parent A (shared calendar)",
                },
            ]
        },
    )
    with _client(tmp_path / "x.sqlite3") as client:
        tb = client.get("/api/family").json()["travel_blocks"]
    assert tb["duplicate_calendars"] == ["Parent A (shared calendar)"]


def test_travel_blocks_reports_write_token_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors ``token_present``: no write token is why a live sweep writes nothing."""
    _isolated_config(tmp_path, monkeypatch)
    with _client(tmp_path / "x.sqlite3") as client:
        assert client.get("/api/family").json()["travel_blocks"]["write_token_present"] is False

    token = tmp_path / "write_token.json"
    token.write_text("{}", encoding="utf-8")
    _isolated_config(tmp_path, monkeypatch, calendar={"write_token_path": str(token)})
    with _client(tmp_path / "x.sqlite3") as client:
        assert client.get("/api/family").json()["travel_blocks"]["write_token_present"] is True


def test_write_capability_reports_all_three_states(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``unknown`` is its own reported state — never omitted, never a pass.

    Parent C is deliberately absent from the sweep's capability map: the probe
    never resolved for that calendar, and the payload has to say so rather than
    drop the row (which would read as "no problem") or default it to writable.
    """
    _isolated_config(tmp_path, monkeypatch, calendar={"accounts": _ACCOUNTS})
    db = tmp_path / "x.sqlite3"
    _seed_calendar_scan(db, _ok_sweep(apply={
        "status": "applied",
        "counts": {"inserted": 2, "deleted": 1, "kept": 1, "skipped": 0, "backups": 1},
        "write_capability": {
            "parent-a@example.com": "writable",
            "parent-b@example.com": "not_writable",
        },
        "failures": [],
    }))
    with _client(db) as client:
        rows = client.get("/api/family").json()["travel_blocks"]["write_capability"]
    assert [row["person"] for row in rows] == ["parent-a", "parent-b", "parent-c"]
    assert [row["state"] for row in rows] == ["writable", "not_writable", "unknown"]
    assert [row["label"] for row in rows] == ["Parent A", "Parent B", "Parent C"]


def test_write_capability_defaults_to_unknown_with_no_sweep(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No sweep has run at all: every calendar is unestablished, not writable."""
    _isolated_config(tmp_path, monkeypatch, calendar={"accounts": _ACCOUNTS})
    with _client(tmp_path / "x.sqlite3") as client:
        rows = client.get("/api/family").json()["travel_blocks"]["write_capability"]
    assert len(rows) == 3
    assert {row["state"] for row in rows} == {"unknown"}


def test_unrecognized_capability_value_reads_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A state this build does not know is unestablished — never folded into writable."""
    _isolated_config(tmp_path, monkeypatch, calendar={"accounts": _ACCOUNTS[:1]})
    db = tmp_path / "x.sqlite3"
    _seed_calendar_scan(db, _ok_sweep(apply={
        "status": "applied",
        "counts": {},
        "write_capability": {"parent-a@example.com": "probably-fine"},
        "failures": [],
    }))
    with _client(db) as client:
        rows = client.get("/api/family").json()["travel_blocks"]["write_capability"]
    assert rows[0]["state"] == "unknown"


def test_last_sweep_summary_carries_plan_and_apply_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_config(tmp_path, monkeypatch, calendar={"accounts": _ACCOUNTS[:1]})
    db = tmp_path / "x.sqlite3"
    run_id = _seed_calendar_scan(db, _ok_sweep(dry_run=False, apply={
        "status": "applied",
        "counts": {"inserted": 2, "deleted": 1, "kept": 1, "skipped": 0, "backups": 1},
        "write_capability": {"parent-a@example.com": "writable"},
        "failures": [{"operation": "insert", "reason": "insert_failed"}],
    }))
    with _client(db) as client:
        sweep = client.get("/api/family").json()["travel_blocks"]["last_sweep"]
    assert sweep["run_id"] == f"db-{run_id}"
    assert sweep["status"] == "ok"
    assert sweep["dry_run"] is False
    assert sweep["routes_calls"] == 3
    assert sweep["counts"] == {
        "desired": 4, "adds": 2, "deletes": 1, "keeps": 1, "protected": 0, "failures": 1,
    }
    assert sweep["apply"]["status"] == "applied"
    assert sweep["apply"]["counts"]["inserted"] == 2
    assert sweep["apply"]["failures"] == 1


def test_dry_run_sweep_is_reported_as_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A planning-only sweep must never be readable as one that wrote."""
    _isolated_config(tmp_path, monkeypatch)
    db = tmp_path / "x.sqlite3"
    _seed_calendar_scan(db, _ok_sweep(apply={
        "status": "dry_run",
        "counts": {"inserted": 0, "deleted": 0, "kept": 1, "skipped": 3, "backups": 0},
        "write_capability": {},
        "failures": [],
    }))
    with _client(db) as client:
        sweep = client.get("/api/family").json()["travel_blocks"]["last_sweep"]
    assert sweep["dry_run"] is True
    assert sweep["apply"]["status"] == "dry_run"
    assert sweep["apply"]["counts"]["inserted"] == 0


def test_gated_sweep_reports_its_status_without_fabricated_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``disabled`` computed nothing — zeros would read like a computed all-clear."""
    _isolated_config(tmp_path, monkeypatch)
    db = tmp_path / "x.sqlite3"
    _seed_calendar_scan(db, {"status": "disabled"})
    with _client(db) as client:
        sweep = client.get("/api/family").json()["travel_blocks"]["last_sweep"]
    assert sweep["status"] == "disabled"
    assert sweep["counts"] is None
    assert sweep["dry_run"] is None
    assert sweep["apply"] is None


def test_calendar_scan_run_without_a_sweep_section_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runs recorded before the feature existed are not "the last sweep"."""
    _isolated_config(tmp_path, monkeypatch)
    db = tmp_path / "x.sqlite3"
    _seed_calendar_scan(db, None)
    with _client(db) as client:
        assert client.get("/api/family").json()["travel_blocks"]["last_sweep"] is None


def test_family_tab_renders_from_one_store_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No second query for the sweep — and emphatically no sweep of its own."""
    _isolated_config(tmp_path, monkeypatch, calendar={"accounts": _ACCOUNTS})
    db = tmp_path / "x.sqlite3"
    _seed_calendar_scan(db, _ok_sweep())
    calls: list[int] = []
    real = store.list_review_runs
    monkeypatch.setattr(
        store,
        "list_review_runs",
        lambda conn, limit: calls.append(limit) or real(conn, limit),
    )
    with _client(db) as client:
        assert client.get("/api/family").status_code == 200
    assert len(calls) == 1


# ------------------------------------- travel blocks: POST validation (#268)


@pytest.mark.parametrize("value", [True, False])
def test_travel_blocks_toggles_round_trip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: bool
) -> None:
    saved = _patched_save(monkeypatch)
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post(
            "/api/family",
            json={"travel_blocks_enabled": value, "travel_blocks_dry_run": value},
        )
    assert res.status_code == 200
    assert saved["family"]["travel_blocks"] == {"enabled": value, "dry_run": value}


@pytest.mark.parametrize("value", [0, 45, 480])
def test_min_home_dwell_min_in_range_saves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: int
) -> None:
    saved = _patched_save(monkeypatch)
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"min_home_dwell_min": value})
    assert res.status_code == 200
    assert saved["family"]["travel_blocks"]["min_home_dwell_min"] == value


@pytest.mark.parametrize("bad", [-1, 481, 10000])
def test_min_home_dwell_min_out_of_range_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: int
) -> None:
    _patched_save(monkeypatch)
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"min_home_dwell_min": bad})
    assert res.status_code == 400
    assert "min_home_dwell_min" in res.json()["detail"]


def test_title_template_saves_stripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = _patched_save(monkeypatch)
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"title_template": "  Commute  "})
    assert res.status_code == 200
    assert saved["family"]["travel_blocks"]["title_template"] == "Commute"


@pytest.mark.parametrize("bad", ["", "   ", "\t\n"])
def test_blank_title_template_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """A blank title would put an unlabelled block on a shared calendar."""
    _patched_save(monkeypatch)
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"title_template": bad})
    assert res.status_code == 400
    assert "title_template" in res.json()["detail"]


def test_over_long_title_template_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patched_save(monkeypatch)
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"title_template": "x" * 61})
    assert res.status_code == 400
    assert "title_template" in res.json()["detail"]


def test_schedule_save_leaves_travel_blocks_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A schedule-only POST must not write a ``travel_blocks`` key at all.

    The two cards save independently; an empty sub-dict deep-merged in would be
    harmless today but would let a future partial write clobber the knobs.
    """
    saved = _patched_save(monkeypatch)
    with _client(tmp_path / "x.sqlite3") as client:
        res = client.post("/api/family", json={"kids_home_time": "17:45"})
    assert res.status_code == 200
    assert "travel_blocks" not in saved["family"]
