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

# Telegram's `sendMessage` API doc: "Text of the message to be sent, 1-4096
# characters" — an unbounded digest (e.g. the first scan after a multi-day
# outage) exceeds this and every retry fails identically (#435).
_MAX_MESSAGE_CHARS = 4096


def _chunk_text(text: str, limit: int) -> list[str]:
    """Split ``text`` into pieces of at most ``limit`` characters, exactly.

    Cuts at (and keeps) the last newline at or before ``limit`` so a line is
    never split mid-word and ``"".join(_chunk_text(text, limit)) == text`` —
    no content lost at a chunk boundary. A single line longer than ``limit``
    is hard-cut as a last resort.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        cut = cut + 1 if cut > 0 else limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


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
        self.send_text(digest.to_telegram_text())

    def send_text(self, text: str) -> None:
        """Deliver a plain-text message to the configured chat.

        Used both for the digest and for out-of-band operational alerts (e.g. a
        scan aborted because the WhatsApp source went offline), so a scheduled
        run that would otherwise look green still reaches the operator. Sent as
        one or more ``sendMessage`` calls, chunked under Telegram's per-message
        limit (#435) — a single oversized digest must not fail delivery outright.
        """
        for chunk in _chunk_text(text, _MAX_MESSAGE_CHARS):
            self._send_one(chunk)

    def _send_one(self, text: str) -> None:
        payload = json.dumps({"chat_id": self._chat_id, "text": text}).encode("utf-8")
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
