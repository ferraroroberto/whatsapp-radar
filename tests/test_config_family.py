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
                        "quiet_start_hour": 21, "quiet_end_hour": 6, "cadence_min": 45},
            "family": {
                "enabled": True,
                "home_address": "Home 1",
                "responsible_by_weekday": {"mon": "roberto", "fri": "ana"},
                "childcare_windows": [
                    {"label": "swim", "weekdays": ["mon", "wed"], "time": "16:45"}
                ],
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

    assert cfg.family.enabled
    assert cfg.family.home_address == "Home 1"
    # weekday names are normalized to 0=Mon indices
    assert cfg.family.responsible_by_weekday == {0: "roberto", 4: "ana"}
    assert cfg.family.childcare_windows[0].weekdays == (0, 2)

    assert cfg.calendar.accounts[0].calendar_id == "a@x"
    assert cfg.calendar.accounts[0].person == "roberto"  # lowercased


def test_family_defaults_disabled(tmp_path, _clean_env):
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "default.json").write_text(
        json.dumps({"db_path": "data/x.sqlite3"}), encoding="utf-8"
    )
    cfg = load_config(root=tmp_path)
    assert cfg.traffic.enabled is False
    assert cfg.traffic.cadence_min == 30  # new #164 default
    assert cfg.family.enabled is False
    assert cfg.calendar.accounts == ()
