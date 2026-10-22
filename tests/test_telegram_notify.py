"""Telegram delivery: digest rendering, payload, success/failure — all offline.

No real token, chat, or network: urlopen is monkeypatched.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any

import pytest

from src.config import TelegramConfig
from src.notify import NotifierError, TelegramNotifier, build_notifier
from src.notify import telegram as telegram_mod
from src.report.digest import Digest, DigestItem


def _digest() -> Digest:
    return Digest(
        run_id=7,
        items=[
            DigestItem(
                chat="Class 4A Group",
                priority="high",
                summary="Bring signed permission slip Friday",
                suggested_next_action="Sign and return the slip",
                deadline="Friday",
                confidence=0.9,
                evidence_message_ids=["A"],
            )
        ],
    )


def test_to_telegram_text_contains_essentials() -> None:
    text = _digest().to_telegram_text()
    assert "Class 4A Group" in text
    assert "high" in text
    assert "Bring signed permission slip Friday" in text
    assert "Friday" in text


def test_empty_digest_text() -> None:
    assert "no actionable items" in Digest(run_id=1, items=[]).to_telegram_text().lower()


def test_build_notifier_branches() -> None:
    assert build_notifier("none", TelegramConfig("", "")) is None
    assert isinstance(
        build_notifier("telegram", TelegramConfig("t", "c")), TelegramNotifier
    )
    with pytest.raises(ValueError):
        build_notifier("carrier-pigeon", TelegramConfig("t", "c"))


def test_telegram_requires_credentials() -> None:
    with pytest.raises(NotifierError):
        TelegramNotifier("", "")


@contextmanager
def _fake_response(payload: dict[str, Any]):
    class _Resp:
        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

    yield _Resp()


def test_send_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: int = 0):  # noqa: ANN401
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _fake_response({"ok": True})

    monkeypatch.setattr(telegram_mod.urllib.request, "urlopen", fake_urlopen)
    TelegramNotifier("TOKEN", "CHAT").send(_digest())

    assert "botTOKEN/sendMessage" in captured["url"]
    assert captured["body"]["chat_id"] == "CHAT"
    assert "Class 4A Group" in captured["body"]["text"]


def test_send_rejected_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(request: Any, timeout: int = 0):  # noqa: ANN401
        return _fake_response({"ok": False, "description": "chat not found"})

    monkeypatch.setattr(telegram_mod.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(NotifierError, match="chat not found"):
        TelegramNotifier("TOKEN", "CHAT").send(_digest())


def test_send_text_under_the_limit_is_one_call(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []

    def fake_urlopen(request: Any, timeout: int = 0):  # noqa: ANN401
        sent.append(json.loads(request.data.decode("utf-8"))["text"])
        return _fake_response({"ok": True})

    monkeypatch.setattr(telegram_mod.urllib.request, "urlopen", fake_urlopen)
    TelegramNotifier("TOKEN", "CHAT").send_text("short message")

    assert sent == ["short message"]


def test_send_text_over_the_limit_is_chunked(monkeypatch: pytest.MonkeyPatch) -> None:
    """#435: Telegram's `sendMessage` rejects text over 4096 characters — an
    unbounded digest (e.g. the first scan after a multi-day outage) must split
    rather than fail delivery outright on every retry.
    """
    sent: list[str] = []

    def fake_urlopen(request: Any, timeout: int = 0):  # noqa: ANN401
        sent.append(json.loads(request.data.decode("utf-8"))["text"])
        return _fake_response({"ok": True})

    monkeypatch.setattr(telegram_mod.urllib.request, "urlopen", fake_urlopen)
    # One line per item, well past the 4096-char limit.
    big_text = "\n".join(f"item {i}: " + "x" * 80 for i in range(100))
    assert len(big_text) > telegram_mod._MAX_MESSAGE_CHARS

    TelegramNotifier("TOKEN", "CHAT").send_text(big_text)

    assert len(sent) > 1
    assert all(len(chunk) <= telegram_mod._MAX_MESSAGE_CHARS for chunk in sent)
    assert "".join(sent) == big_text  # no content dropped at a chunk boundary


def test_chunk_text_never_splits_a_line_when_one_fits() -> None:
    text = "line one\n" + "x" * 4090 + "\nline three"
    chunks = telegram_mod._chunk_text(text, 4096)
    assert all("line one" not in c or c.startswith("line one") for c in chunks)
    assert "".join(chunks).count("line three") == 1
