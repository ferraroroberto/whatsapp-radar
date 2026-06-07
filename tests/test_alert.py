"""Operational alerts (src/notify/alert.py): best-effort, never raises.

Covers the three outcomes send_alert can report — skipped (no/!text channel),
sent (Telegram, urlopen stubbed so no network), and failed (misconfigured) — so
the offline-scan abort path (#29) can fire it without any risk of raising.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.config import Config, HubConfig, TelegramConfig
from src.notify import telegram as telegram_mod
from src.notify.alert import send_alert


def _config(*, notifier: str, token: str = "t", chat: str = "c") -> Config:
    return Config(
        db_path="unused.sqlite3",  # type: ignore[arg-type]
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="m"),
        notifier=notifier,
        telegram=TelegramConfig(bot_token=token, chat_id=chat),
        linked_device_dir="ld",  # type: ignore[arg-type]
    )


def test_alert_skipped_when_no_notifier() -> None:
    status, _ = send_alert(_config(notifier="none"), "hi")
    assert status == "skipped"


def test_alert_sent_via_telegram(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: int = 0):  # noqa: ANN401
        sent["text"] = json.loads(request.data.decode("utf-8"))["text"]

        class _Resp:
            def read(self) -> bytes:
                return json.dumps({"ok": True}).encode("utf-8")

        class _Ctx:
            def __enter__(self) -> _Resp:
                return _Resp()

            def __exit__(self, *exc: object) -> None:
                return None

        return _Ctx()

    monkeypatch.setattr(telegram_mod.urllib.request, "urlopen", fake_urlopen)
    status, detail = send_alert(_config(notifier="telegram"), "source offline")
    assert status == "sent" and detail is None
    assert sent["text"] == "source offline"


def test_alert_failed_when_misconfigured() -> None:
    # Telegram selected but no token/chat → notifier construction fails; reported
    # as 'failed', never raised.
    status, detail = send_alert(_config(notifier="telegram", token="", chat=""), "x")
    assert status == "failed"
    assert detail
