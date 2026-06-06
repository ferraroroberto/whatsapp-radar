"""Telegram delivery via the Bot API.

Sends the consolidated digest to a single configured chat using the Bot API
``sendMessage`` endpoint. Uses the standard library only (``urllib``) so the
project takes on no extra dependency for one HTTPS POST.

The bot token and chat id are secrets: they come from ignored config
(``WR_TELEGRAM_BOT_TOKEN`` / ``WR_TELEGRAM_CHAT_ID`` or ``config/local.json``)
and are never committed.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from src.notify.base import NotifierError
from src.report.digest import Digest

_API = "https://api.telegram.org/bot{token}/sendMessage"
_TIMEOUT_SECONDS = 15


class TelegramNotifier:
    """A :class:`Notifier` that delivers the digest text to one Telegram chat."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        if not bot_token or not chat_id:
            raise NotifierError(
                "Telegram notifier needs WR_TELEGRAM_BOT_TOKEN and WR_TELEGRAM_CHAT_ID"
            )
        self._token = bot_token
        self._chat_id = chat_id

    def send(self, digest: Digest) -> None:
        payload = json.dumps(
            {"chat_id": self._chat_id, "text": digest.to_telegram_text()}
        ).encode("utf-8")
        request = urllib.request.Request(
            _API.format(token=self._token),
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise NotifierError(f"Telegram request failed: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise NotifierError("Telegram returned a non-JSON response") from exc
        if not body.get("ok"):
            raise NotifierError(f"Telegram rejected the message: {body.get('description')}")
