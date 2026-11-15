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


def test_public_base_url_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Env > ``public_base_url`` > the passkey origin; a trailing slash is dropped."""
    root = _root_with_default(tmp_path, "b", "c")
    monkeypatch.delenv("WR_PUBLIC_BASE_URL", raising=False)

    def webapp(**fields: str) -> None:
        monkeypatch.setattr(
            "src.webapp_config.load_webapp_config", lambda: WebappConfig(**fields)
        )

    webapp()
    assert load_config(root).public_base_url == ""
    webapp(webauthn_origin="https://pc.example.ts.net:8455")
    assert load_config(root).public_base_url == "https://pc.example.ts.net:8455"
    webapp(webauthn_origin="https://a.example", public_base_url="https://b.example/")
    assert load_config(root).public_base_url == "https://b.example"
    monkeypatch.setenv("WR_PUBLIC_BASE_URL", "https://c.example")
    assert load_config(root).public_base_url == "https://c.example"


def test_public_base_url_must_be_http(tmp_path: Path) -> None:
    from src.webapp_config import load_webapp_config

    path = tmp_path / "webapp_config.json"
    path.write_text(json.dumps({"public_base_url": "ftp://x"}), encoding="utf-8")
    # An invalid value is rejected on load rather than put in a message.
    with pytest.raises(ValueError, match="public_base_url"):
        load_webapp_config(path)
