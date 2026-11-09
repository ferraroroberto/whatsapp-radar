"""Digest delivery over the vendored notifier — all offline.

``urlopen`` is stubbed, so no token, chat or network is involved and no message
is ever sent. Covers the three outcomes ``deliver_digest`` records (sent, failed,
skipped) and that a digest past Telegram's per-message limit is chunked, not lost.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any

import pytest

from src.config import Config, HubConfig, TelegramConfig
from src.db import store
from src.delivery import deliver_digest
from src.notify import telegram as telegram_mod
from src.report.digest import Digest, DigestItem


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


def _item(n: int) -> DigestItem:
    return DigestItem(
        chat="Class 4A Group",
        priority="high",
        summary=f"Bring signed permission slip {n} " + "x" * 120,
        suggested_next_action="Sign and return the slip",
        deadline="Friday",
        confidence=0.9,
        evidence_message_ids=[f"M{n}"],
    )


@contextmanager
def _fake_response(payload: dict[str, Any]):
    class _Resp:
        def read(self) -> bytes:
            return json.dumps(payload).encode("utf-8")

    yield _Resp()


def _recorded(conn: sqlite3.Connection, run_id: int) -> tuple[str, str, str | None]:
    row = conn.execute(
        "SELECT channel, status, error FROM notifications WHERE run_id = ?", (run_id,)
    ).fetchone()
    return row["channel"], row["status"], row["error"]


def test_deliver_digest_sends_and_records(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[dict[str, Any]] = []

    def fake_urlopen(request: Any, timeout: int = 0):  # noqa: ANN401
        sent.append(json.loads(request.data.decode("utf-8")))
        return _fake_response({"ok": True})

    monkeypatch.setattr(telegram_mod.urllib.request, "urlopen", fake_urlopen)
    run_id = store.start_run(conn)
    digest = Digest(run_id=run_id, items=[_item(1)])

    status, detail = deliver_digest(conn, _config(notifier="telegram"), run_id, digest)

    assert (status, detail) == ("sent", None)
    assert len(sent) == 1
    assert sent[0]["chat_id"] == "c"
    assert sent[0]["text"] == digest.to_telegram_text()
    assert _recorded(conn, run_id) == ("telegram", "sent", None)


def test_deliver_digest_chunks_a_long_digest(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[str] = []

    def fake_urlopen(request: Any, timeout: int = 0):  # noqa: ANN401
        sent.append(json.loads(request.data.decode("utf-8"))["text"])
        return _fake_response({"ok": True})

    monkeypatch.setattr(telegram_mod.urllib.request, "urlopen", fake_urlopen)
    run_id = store.start_run(conn)
    digest = Digest(run_id=run_id, items=[_item(n) for n in range(60)])
    text = digest.to_telegram_text()
    assert len(text) > telegram_mod._MAX_MESSAGE_CHARS

    status, _ = deliver_digest(conn, _config(notifier="telegram"), run_id, digest)

    assert status == "sent"
    assert len(sent) > 1
    assert all(len(piece) <= telegram_mod._MAX_MESSAGE_CHARS for piece in sent)
    assert "".join(sent) == text  # nothing dropped at a chunk boundary


def test_deliver_digest_records_a_rejected_send(
    conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_urlopen(request: Any, timeout: int = 0):  # noqa: ANN401
        return _fake_response({"ok": False, "description": "chat not found"})

    monkeypatch.setattr(telegram_mod.urllib.request, "urlopen", fake_urlopen)
    run_id = store.start_run(conn)

    status, detail = deliver_digest(
        conn, _config(notifier="telegram"), run_id, Digest(run_id=run_id, items=[_item(1)])
    )

    assert status == "failed"
    assert detail and "chat not found" in detail
    assert _recorded(conn, run_id) == ("telegram", "failed", detail)


def test_deliver_digest_skipped_without_a_notifier(conn: sqlite3.Connection) -> None:
    run_id = store.start_run(conn)

    status, detail = deliver_digest(
        conn, _config(notifier="none"), run_id, Digest(run_id=run_id, items=[_item(1)])
    )

    assert status == "skipped"
    assert detail
    assert _recorded(conn, run_id)[1] == "skipped"


def test_deliver_digest_without_credentials_never_reaches_the_network(
    conn: sqlite3.Connection,
) -> None:
    run_id = store.start_run(conn)

    status, detail = deliver_digest(
        conn,
        _config(notifier="telegram", token="", chat=""),
        run_id,
        Digest(run_id=run_id, items=[_item(1)]),
    )

    assert status == "failed"
    assert detail
