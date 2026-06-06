"""Telegram secret precedence: WR_* env > webapp_config.json > default.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import load_config
from src.webapp_config import WebappConfig


def _root_with_default(tmp_path: Path, bot: str, chat: str) -> Path:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.json").write_text(
        json.dumps({"telegram": {"bot_token": bot, "chat_id": chat}}),
        encoding="utf-8",
    )
    return tmp_path


def _patch_webapp(monkeypatch: pytest.MonkeyPatch, bot: str, chat: str) -> None:
    monkeypatch.setattr(
        "src.webapp_config.load_webapp_config",
        lambda: WebappConfig(telegram_bot_token=bot, telegram_chat_id=chat),
    )


def test_webapp_config_overrides_default_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("WR_TELEGRAM_CHAT_ID", raising=False)
    root = _root_with_default(tmp_path, "from-default", "from-default")
    _patch_webapp(monkeypatch, "from-webapp", "from-webapp")

    cfg = load_config(root)
    assert cfg.telegram.bot_token == "from-webapp"
    assert cfg.telegram.chat_id == "from-webapp"


def test_env_overrides_webapp_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WR_TELEGRAM_BOT_TOKEN", "from-env")
    monkeypatch.delenv("WR_TELEGRAM_CHAT_ID", raising=False)
    root = _root_with_default(tmp_path, "from-default", "from-default")
    _patch_webapp(monkeypatch, "from-webapp", "from-webapp")

    cfg = load_config(root)
    assert cfg.telegram.bot_token == "from-env"  # env wins
    assert cfg.telegram.chat_id == "from-webapp"  # webapp wins over default


def test_falls_back_to_default_when_webapp_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("WR_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("WR_TELEGRAM_CHAT_ID", raising=False)
    root = _root_with_default(tmp_path, "from-default", "from-default")
    _patch_webapp(monkeypatch, "", "")

    cfg = load_config(root)
    assert cfg.telegram.bot_token == "from-default"
    assert cfg.telegram.chat_id == "from-default"
