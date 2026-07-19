"""Family rules command center: /api/family POST validation (issue #167).

Every rule in the Family tab's "Rules in force" card is editable and server-
validated before it lands in config/local.json: times parse as HH:MM, an
on-duty pattern names exactly the 7 weekdays, and a childcare window's
optional end must come after its start (non-inverted). Offline throughout —
`save_local_overrides` is monkeypatched so nothing touches the real,
gitignored config/local.json.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import app.webapp.routers.family as family_router
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
