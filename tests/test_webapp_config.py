"""Webapp config: load/save round-trip + validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.webapp_config import WebappConfig, load_webapp_config, save_webapp_config


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    cfg = load_webapp_config(tmp_path / "nope.json")
    assert cfg.port == 8455
    assert cfg.auth_token == ""
    assert cfg.webauthn_rp_name == "WhatsApp Radar"
    assert cfg.tailnet_allowlist == []
    assert cfg.sender_voice_genders == {}
    assert cfg.default_voice_gender == "female"


def test_save_load_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "webapp_config.json"
    cfg = WebappConfig(
        port=8455,
        auth_token="tok",
        auth_password="pw",
        tailnet_allowlist=["192.168.1.0/24"],
        webauthn_rp_id="pc.tailnet.ts.net",
        webauthn_origin="https://pc.tailnet.ts.net:8455",
        telegram_bot_token="bot",
        telegram_chat_id="chat",
        sender_voice_genders={"teacher": "female", "dad": "male"},
        default_voice_gender="male",
    )
    save_webapp_config(cfg, target)
    loaded = load_webapp_config(target)
    assert loaded == cfg


def test_validate_rejects_bad_default_voice_gender(tmp_path: Path) -> None:
    target = tmp_path / "webapp_config.json"
    target.write_text('{"default_voice_gender": "nonbinary"}', encoding="utf-8")
    with pytest.raises(ValueError, match="invalid default_voice_gender"):
        load_webapp_config(target)


def test_validate_rejects_bad_sender_voice_gender(tmp_path: Path) -> None:
    target = tmp_path / "webapp_config.json"
    target.write_text(
        '{"sender_voice_genders": {"teacher": "robot"}}', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="invalid voice gender"):
        load_webapp_config(target)


def test_sender_voice_genders_normalized_on_load(tmp_path: Path) -> None:
    target = tmp_path / "webapp_config.json"
    target.write_text(
        '{"sender_voice_genders": {"  Teacher  ": "female"}}', encoding="utf-8"
    )
    cfg = load_webapp_config(target)
    assert cfg.sender_voice_genders == {"teacher": "female"}


def test_validate_rejects_bad_port(tmp_path: Path) -> None:
    target = tmp_path / "webapp_config.json"
    target.write_text('{"port": 70000}', encoding="utf-8")
    with pytest.raises(ValueError, match="port out of range"):
        load_webapp_config(target)


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    target = tmp_path / "webapp_config.json"
    target.write_text('{"port": 8455, "_comment": "hi", "legacy": 1}', encoding="utf-8")
    cfg = load_webapp_config(target)
    assert cfg.port == 8455
