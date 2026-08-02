"""Config parsing for the family-check sections (issue #160)."""

from __future__ import annotations

import json

import pytest

from src.config import load_config

_ENV_KEYS = (
    "WR_TRAFFIC_ENABLED",
    "WR_TRAFFIC_API_KEY",
    "GOOGLE_MAPS_API_KEY",
    "WR_FAMILY_ENABLED",
    "WR_CALENDAR_TOKEN_PATH",
    "WR_CALENDAR_CREDENTIALS_PATH",
    "WR_CALENDAR_WRITE_TOKEN_PATH",
    "WR_PRESENCE_ENABLED",
    "WR_PRESENCE_BASE_URL",
)


@pytest.fixture
def _clean_env(monkeypatch):
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_family_config_parsing(tmp_path, _clean_env):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "default.json").write_text(
        json.dumps({"db_path": "data/x.sqlite3", "calendar": {"accounts": []},
                    "traffic": {"enabled": False}, "family": {"enabled": False}}),
        encoding="utf-8",
    )
    (cfg_dir / "local.json").write_text(
        json.dumps({
            "calendar": {"accounts": [{"calendar_id": "a@x", "person": "Roberto", "label": "R"}]},
            "traffic": {"enabled": True, "api_key": "k", "significant_delay_min": 20,
                        "quiet_start_hour": 21, "quiet_end_hour": 6, "cadence_min": 45,
                        "skip_leave_now_for_train": False, "train_keywords": ["Tren", " "]},
            "family": {
                "enabled": True,
                "home_address": "Home 1",
                "responsible_by_weekday": {"mon": "roberto", "fri": "ana"},
                "childcare_windows": [
                    {"label": "swim", "weekdays": ["mon", "wed"], "time": "16:45"},
                    {
                        "label": "after-school club",
                        "weekdays": ["tue"],
                        "time": "15:30",
                        "end_time": "17:00",
                    },
                ],
                "reminder_calendar_id": "family@example.test",
                "reminder_time": "08:00",
            },
        }),
        encoding="utf-8",
    )

    cfg = load_config(root=tmp_path)

    assert cfg.traffic.enabled
    assert cfg.traffic.api_key == "k"
    assert cfg.traffic.significant_delay_min == 20
    assert (cfg.traffic.quiet_start_hour, cfg.traffic.quiet_end_hour) == (21, 6)
    assert cfg.traffic.cadence_min == 45
    # #227: the toggle is overridable and keywords are normalized (lowercased,
    # blanks dropped) so a hand-edited local.json can't match every event.
    assert cfg.traffic.skip_leave_now_for_train is False
    assert cfg.traffic.train_keywords == ("tren",)

    assert cfg.family.enabled
    assert cfg.family.home_address == "Home 1"
    # weekday names are normalized to 0=Mon indices
    assert cfg.family.responsible_by_weekday == {0: "roberto", 4: "ana"}
    assert cfg.family.childcare_windows[0].weekdays == (0, 2)
    assert cfg.family.childcare_windows[0].end_time == ""  # point-in-time deadline, back-compat
    assert cfg.family.childcare_windows[1].end_time == "17:00"  # a genuine range (#167)

    assert cfg.calendar.accounts[0].calendar_id == "a@x"
    assert cfg.calendar.accounts[0].person == "roberto"  # lowercased

    # Routine-prep calendar reminders (#218)
    assert cfg.family.reminder_calendar_id == "family@example.test"
    assert cfg.family.reminder_time == "08:00"


def test_family_defaults_disabled(tmp_path, _clean_env):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "default.json").write_text(
        json.dumps({"db_path": "data/x.sqlite3"}), encoding="utf-8"
    )
    cfg = load_config(root=tmp_path)
    assert cfg.traffic.enabled is False
    assert cfg.traffic.cadence_min == 30  # new #164 default
    assert cfg.traffic.skip_leave_now_for_train is True  # #227 on by default
    assert cfg.traffic.train_keywords == ("tren", "train")
    assert cfg.family.enabled is False
    assert cfg.family.reminder_calendar_id == ""  # feature off by default (#218)
    assert cfg.family.reminder_time == "07:30"
    assert cfg.calendar.accounts == ()
    # Write-scope token (#217) defaults to a sibling path of the read-only token.
    assert cfg.calendar.write_token_path.as_posix().endswith("auth/calendar/write_token.json")
    # Presence (#169) defaults: disabled, loopback home-automation, 5-min freshness.
    assert cfg.presence.enabled is False
    assert cfg.presence.base_url == "http://127.0.0.1:8447"
    assert cfg.presence.max_age_min == 5
    assert cfg.presence.person_aliases == {}


def test_calendar_write_token_path_override(tmp_path, _clean_env):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "default.json").write_text(
        json.dumps({"db_path": "data/x.sqlite3"}), encoding="utf-8"
    )
    (cfg_dir / "local.json").write_text(
        json.dumps({"calendar": {"write_token_path": "auth/calendar/custom_write.json"}}),
        encoding="utf-8",
    )
    cfg = load_config(root=tmp_path)
    assert cfg.calendar.write_token_path == tmp_path / "auth" / "calendar" / "custom_write.json"


def test_presence_config_parsing(tmp_path, _clean_env):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "default.json").write_text(
        json.dumps({"db_path": "data/x.sqlite3"}), encoding="utf-8"
    )
    (cfg_dir / "local.json").write_text(
        json.dumps({
            "presence": {
                "enabled": True,
                "base_url": "http://127.0.0.1:9999",
                "max_age_min": 3,
                "timeout_s": 4,
                "person_aliases": {"Roberto": ["dad"], "ana": "mom"},
            }
        }),
        encoding="utf-8",
    )
    cfg = load_config(root=tmp_path)
    assert cfg.presence.enabled is True
    assert cfg.presence.base_url == "http://127.0.0.1:9999"
    assert cfg.presence.max_age_min == 3
    assert cfg.presence.timeout_s == 4.0
    # Person keys lowercased; a scalar alias is normalized to a tuple.
    assert cfg.presence.person_aliases == {"roberto": ("dad",), "ana": ("mom",)}


# ---------------------------------------------------------------- issue #252


def test_dedup_window_is_floored_at_the_lookahead() -> None:
    """A window shorter than the lookahead guarantees duplicate alerts.

    One event stays inside the lookahead for `lookahead_hours` and is
    re-checked every `cadence_min` throughout, so anything shorter mathematically
    re-alerts. The reported case was `dedup_window_min == cadence_min == 30`,
    which put each record exactly on the window boundary and produced four
    "Tight schedule" messages for one event.
    """
    from src.config.traffic import TrafficConfig

    degenerate = TrafficConfig(dedup_window_min=30, cadence_min=30, lookahead_hours=3)
    assert degenerate.effective_dedup_window_min == 180

    # A window already longer than the lookahead is honoured as configured.
    generous = TrafficConfig(dedup_window_min=360, cadence_min=30, lookahead_hours=3)
    assert generous.effective_dedup_window_min == 360

    # The floor tracks the lookahead rather than being a hardcoded constant.
    long_horizon = TrafficConfig(dedup_window_min=30, cadence_min=30, lookahead_hours=8)
    assert long_horizon.effective_dedup_window_min == 480


def test_shipped_defaults_do_not_make_dedup_a_no_op() -> None:
    """The committed default must not be degenerate out of the box."""
    from src.config.traffic import TrafficConfig

    cfg = TrafficConfig()
    assert cfg.dedup_window_min > cfg.cadence_min
    assert cfg.effective_dedup_window_min == cfg.dedup_window_min
