"""Config parsing for the family-check sections (issue #160)."""

from __future__ import annotations

import json

import pytest

from src.config import calendar as calendar_config
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


def test_ask_missing_locations_defaults_to_on(monkeypatch) -> None:
    """An existing config with no such key must behave exactly as before (#253)."""
    from src.config.family import parse

    monkeypatch.delenv("WR_FAMILY_ASK_MISSING_LOCATIONS", raising=False)
    assert parse({}).ask_missing_locations is True
    assert parse({"ask_missing_locations": False}).ask_missing_locations is False
    # The env override wins over the file, mirroring WR_FAMILY_ENABLED.
    monkeypatch.setenv("WR_FAMILY_ASK_MISSING_LOCATIONS", "0")
    assert parse({"ask_missing_locations": True}).ask_missing_locations is False


def test_travel_blocks_config_defaults_are_off_and_dry() -> None:
    """A config with no such block must plan nothing and write nothing (#265)."""
    from src.config.family import parse

    blocks = parse({}).travel_blocks
    assert blocks.enabled is False
    assert blocks.dry_run is True
    assert blocks.horizon_days == 2
    assert blocks.min_home_dwell_min == 45
    assert blocks.title_template == "🚗 Trayecto"


def test_travel_blocks_config_parses_all_five_keys() -> None:
    from src.config.family import parse

    blocks = parse({
        "travel_blocks": {
            "enabled": True,
            "dry_run": False,
            "horizon_days": 3,
            "min_home_dwell_min": 30,
            "title_template": "Commute",
        }
    }).travel_blocks
    assert (blocks.enabled, blocks.dry_run) == (True, False)
    assert (blocks.horizon_days, blocks.min_home_dwell_min) == (3, 30)
    assert blocks.title_template == "Commute"


def test_travel_blocks_ship_disabled_in_committed_defaults() -> None:
    """The committed default.json must never enable a calendar-writing feature."""
    import json

    from src.config import project_root

    raw = json.loads((project_root() / "config" / "default.json").read_text(encoding="utf-8"))
    shipped = raw["family"]["travel_blocks"]
    assert shipped["enabled"] is False
    assert shipped["dry_run"] is True
    assert set(shipped) == {
        "enabled", "dry_run", "horizon_days", "min_home_dwell_min", "title_template",
    }




# ---------------------------------------------------------------- issue #273


@pytest.fixture
def _reset_duplicate_warning_dedup():
    """The once-per-process warning dedup (#273 review finding #1) is
    module-level state, so it survives across tests in the same session —
    reset it before and after each test that inspects the warning, otherwise
    test order decides whether a warning fires.
    """
    calendar_config._WARNED_DUPLICATE_CALENDARS.clear()
    yield
    calendar_config._WARNED_DUPLICATE_CALENDARS.clear()


def test_duplicate_calendar_id_collapses_to_the_first_entry_with_a_warning(
    tmp_path,
    _clean_env,
    _reset_duplicate_warning_dedup,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two accounts sharing one calendar id must never reach the reconcile.

    A duplicate `calendar_id` makes the travel-blocks reconcile's `leg_key`
    collide, churning 2 deletes + 2 inserts forever (#273). Collapsing at
    config-parse time — rather than refusing to boot, which would take down a
    live app for a household whose existing `config/local.json` already has
    the mistake — is the chosen fix; this pins that the collapse actually
    happens, keeps the *first* entry, logs a warning naming the calendar by
    label (never its raw id), and names the coverage-roster consequence for
    the dropped person.
    """
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "default.json").write_text(
        json.dumps({"db_path": "data/x.sqlite3"}), encoding="utf-8"
    )
    (cfg_dir / "local.json").write_text(
        json.dumps({
            "calendar": {
                "accounts": [
                    {
                        "calendar_id": "shared@example.test",
                        "person": "parent-a",
                        "label": "Parent A",
                    },
                    {
                        "calendar_id": "shared@example.test",
                        "person": "parent-b",
                        "label": "Parent A (shared calendar)",
                    },
                    {"calendar_id": "solo@example.test", "person": "parent-b", "label": "Parent B"},
                ]
            }
        }),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="src.config.calendar"):
        cfg = load_config(root=tmp_path)

    # Only the first entry for the shared id survives, plus the unrelated one.
    assert [account.label for account in cfg.calendar.accounts] == ["Parent A", "Parent B"]
    assert cfg.calendar.collapsed_duplicate_labels == ("Parent A (shared calendar)",)

    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any(
        "Parent A" in message and "Parent A (shared calendar)" in message for message in warnings
    )
    # Privacy: the raw calendar id never appears in the log line, only labels.
    assert not any("shared@example.test" in message for message in warnings)
    # #273 review finding #3: the dropped person's coverage-roster exposure
    # must be named, not just the churn the fix was written for.
    assert any("coverage" in message.lower() for message in warnings)


def test_the_duplicate_warning_fires_once_per_process_not_once_per_poll(
    tmp_path, _clean_env, _reset_duplicate_warning_dedup, caplog: pytest.LogCaptureFixture
) -> None:
    """#273 review finding #1: `load_config()` is uncached and runs on nearly
    every webapp request. A household carrying the duplicate must see one
    warning, not one per poll — loud is the goal, flooded is its own kind of
    unreadable.
    """
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "default.json").write_text(
        json.dumps({"db_path": "data/x.sqlite3"}), encoding="utf-8"
    )
    (cfg_dir / "local.json").write_text(
        json.dumps({
            "calendar": {
                "accounts": [
                    {
                        "calendar_id": "shared@example.test",
                        "person": "parent-a",
                        "label": "Parent A",
                    },
                    {
                        "calendar_id": "shared@example.test",
                        "person": "parent-b",
                        "label": "Parent B",
                    },
                ]
            }
        }),
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="src.config.calendar"):
        for _ in range(5):
            load_config(root=tmp_path)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1


def test_duplicate_calendar_ids_collapse_case_insensitively(
    tmp_path, _clean_env, _reset_duplicate_warning_dedup
) -> None:
    """#273 review finding #2: Google treats calendar ids case-insensitively,
    so `A@x` and `a@x` are the same collision and must collapse too — the
    surviving account keeps the first entry's *original* spelling, never the
    casefolded form.
    """
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "default.json").write_text(
        json.dumps({"db_path": "data/x.sqlite3"}), encoding="utf-8"
    )
    (cfg_dir / "local.json").write_text(
        json.dumps({
            "calendar": {
                "accounts": [
                    {
                        "calendar_id": "Shared@Example.test",
                        "person": "parent-a",
                        "label": "Parent A",
                    },
                    {
                        "calendar_id": "shared@example.test",
                        "person": "parent-b",
                        "label": "Parent B",
                    },
                ]
            }
        }),
        encoding="utf-8",
    )

    cfg = load_config(root=tmp_path)

    assert len(cfg.calendar.accounts) == 1
    # The first entry's original casing survives — never the casefolded form.
    assert cfg.calendar.accounts[0].calendar_id == "Shared@Example.test"
    assert cfg.calendar.collapsed_duplicate_labels == ("Parent B",)


def test_a_config_with_no_duplicates_reports_none_collapsed(tmp_path, _clean_env) -> None:
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "default.json").write_text(
        json.dumps({"db_path": "data/x.sqlite3", "calendar": {"accounts": []}}), encoding="utf-8"
    )
    (cfg_dir / "local.json").write_text(
        json.dumps({
            "calendar": {
                "accounts": [
                    {"calendar_id": "a@x", "person": "parent-a", "label": "Parent A"},
                    {"calendar_id": "b@x", "person": "parent-b", "label": "Parent B"},
                ]
            }
        }),
        encoding="utf-8",
    )
    cfg = load_config(root=tmp_path)
    assert len(cfg.calendar.accounts) == 2
    assert cfg.calendar.collapsed_duplicate_labels == ()
