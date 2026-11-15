"""Run links on Telegram messages: ``?run=<id>`` appended when a base URL is set — all offline."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Any

import pytest

from src.config import Config, HubConfig, TelegramConfig
from src.db import store
from src.delivery import deliver_digest, send_alert
from src.delivery.links import with_run_link
from src.notify import telegram as telegram_mod
from src.report.digest import Digest, DigestItem

BASE = "https://pc.example.ts.net:8455"


def _config(base: str = BASE) -> Config:
    return Config(
        db_path="unused.sqlite3",  # type: ignore[arg-type]
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="m"),
        notifier="telegram",
        telegram=TelegramConfig(bot_token="t", chat_id="c"),
        linked_device_dir="ld",  # type: ignore[arg-type]
        public_base_url=base,
    )


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    texts: list[str] = []

    def fake_urlopen(request: Any, timeout: int = 0):  # noqa: ANN401
        texts.append(json.loads(request.data.decode("utf-8"))["text"])

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
    return texts


def test_link_is_appended_for_a_run() -> None:
    text = with_run_link(_config(), "body", 42)
    assert text == f"body\n\n🔎 {BASE}/?run=42"


def test_no_link_without_a_base_url_or_a_run() -> None:
    assert with_run_link(_config(base=""), "body", 42) == "body"
    assert with_run_link(_config(), "body", None) == "body"


def test_alert_carries_the_link_only_when_given_a_run(sent: list[str]) -> None:
    assert send_alert(_config(), "traffic delay", run_id=7) == ("sent", None)
    assert send_alert(_config(), "no run here") == ("sent", None)
    assert sent == [f"traffic delay\n\n🔎 {BASE}/?run=7", "no run here"]


def test_alert_without_a_base_url_is_unchanged(sent: list[str]) -> None:
    send_alert(replace(_config(), public_base_url=""), "traffic delay", run_id=7)
    assert sent == ["traffic delay"]


def test_digest_links_to_its_own_run(conn: sqlite3.Connection, sent: list[str]) -> None:
    run_id = store.start_run(conn)
    item = DigestItem(
        chat="Class 4A Group",
        priority="high",
        summary="Bring signed permission slip",
        suggested_next_action="Sign and return the slip",
        deadline="Friday",
        confidence=0.9,
        evidence_message_ids=["M1"],
    )
    digest = Digest(run_id=run_id, items=[item])

    status, _ = deliver_digest(conn, _config(), run_id, digest)

    assert status == "sent"
    assert sent == [f"{digest.to_telegram_text()}\n\n🔎 {BASE}/?run={run_id}"]
