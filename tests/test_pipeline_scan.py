"""Unified scan pipeline: live funnel, dry-run replay, and the audit trace.

All offline: the fixture connector, the deterministic stub, and fake classifiers
stand in for any network. No WhatsApp credentials, no hub, no Telegram.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from src.analysis.classifier import ClassificationOutcome, StubClassifier
from src.analysis.pipeline import scan
from src.config import Config, HubConfig, TelegramConfig
from src.connector.fixture import FixtureConnector
from src.db import store
from src.models import ChatRecord, MessageRecord, StoredMessage
from src.notify import telegram as telegram_mod

from tests.helpers import chat_id_by_source

_ACTIONABLE_JSON = json.dumps(
    {
        "action_required": True,
        "priority": "high",
        "summary": "Pay the fee",
        "suggested_next_action": "Pay it",
        "deadline": "today",
        "confidence": 0.9,
        "evidence_message_ids": ["x"],
    }
)


def _config(tmp_path: Path, *, notifier: str = "none") -> Config:
    return Config(
        db_path=tmp_path / "unused.sqlite3",
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="claude_sonnet"),
        notifier=notifier,
        telegram=TelegramConfig(bot_token="t", chat_id="c"),
        linked_device_dir=tmp_path / "ld",
    )


def _monitor(conn: sqlite3.Connection, source_chat_id: str) -> int:
    chat_id = chat_id_by_source(conn, source_chat_id)
    store.set_chat_status(conn, chat_id, "monitored")
    return chat_id


def _trace(conn: sqlite3.Connection, run_id: int, chat_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM analysis_trace WHERE run_id = ? AND chat_id = ?", (run_id, chat_id)
    ).fetchone()
    assert row is not None
    return row


class _FakeTraced:
    """A Stage-2 classifier that records calls and returns canned trace metadata."""

    def __init__(self, raw_output: str) -> None:
        self.calls = 0
        self._raw = raw_output

    def classify_traced(
        self, chat_display_name: str, delta: list[StoredMessage], prior_context: str | None
    ) -> ClassificationOutcome:
        self.calls += 1
        return ClassificationOutcome(
            raw_output=self._raw,
            llm_called=True,
            system_prompt="SYSTEM-PROMPT",
            user_prompt=f"USER-PROMPT for {chat_display_name}",
            raw_response=f"<think>reasoning</think>{self._raw}",
        )


# --- live funnel -----------------------------------------------------------

def test_live_scan_syncs_analyzes_monitored_and_records_funnel(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _monitor(ingested_conn, "chat-class-4a")
    _monitor(ingested_conn, "chat-school-parents")  # building left unmonitored

    outcome = scan(
        ingested_conn,
        _config(tmp_path),
        mode="live",
        connector=FixtureConnector(),
        classifier=StubClassifier(),
    )

    assert outcome.chats_synced == 3  # all chats synced, monitored and not
    assert outcome.chats_monitored == 2
    assert outcome.chats_with_delta == 2
    assert outcome.stage1_passed == 2
    assert outcome.actionable == 2
    assert outcome.notification_status == "skipped"  # notifier is 'none'
    assert outcome.digest is not None and outcome.digest.has_actionable_items

    # The funnel is persisted on the run row, not just returned.
    row = ingested_conn.execute(
        "SELECT mode, chats_synced, chats_monitored, stage1_passed, actionable, "
        "notification_status FROM review_runs WHERE id = ?",
        (outcome.run_id,),
    ).fetchone()
    assert row["mode"] == "live"
    assert (row["chats_synced"], row["chats_monitored"]) == (3, 2)
    assert (row["stage1_passed"], row["actionable"]) == (2, 2)
    assert row["notification_status"] == "skipped"


def test_live_scan_delivers_when_configured(
    ingested_conn: sqlite3.Connection, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: dict[str, Any] = {}

    def fake_urlopen(request: Any, timeout: int = 0):  # noqa: ANN401
        sent["body"] = json.loads(request.data.decode("utf-8"))

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
    _monitor(ingested_conn, "chat-class-4a")

    outcome = scan(
        ingested_conn,
        _config(tmp_path, notifier="telegram"),
        mode="live",
        connector=FixtureConnector(),
        classifier=StubClassifier(),
    )

    assert outcome.notification_status == "sent"
    assert "Class 4A Group" in sent["body"]["text"]


def test_live_scan_advances_cursor_so_second_run_is_noop(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _monitor(ingested_conn, "chat-class-4a")
    config = _config(tmp_path)

    first = scan(
        ingested_conn, config, mode="live",
        connector=FixtureConnector(), classifier=StubClassifier(),
    )
    assert first.chats_with_delta == 1

    second = scan(
        ingested_conn, config, mode="live",
        connector=FixtureConnector(), classifier=StubClassifier(),
    )
    assert second.chats_with_delta == 0  # cursor advanced after the first run


# --- dry run ---------------------------------------------------------------

def test_dry_run_replays_without_cursor_or_delivery(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    chat_id = _monitor(ingested_conn, "chat-class-4a")

    outcome = scan(ingested_conn, _config(tmp_path), mode="dry_run", classifier=StubClassifier())

    assert outcome.chats_with_delta == 1
    assert outcome.actionable == 1
    assert outcome.notification_status == "dry_run"
    assert outcome.digest is not None and outcome.digest.has_actionable_items

    # No cursor advanced.
    state = ingested_conn.execute(
        "SELECT 1 FROM chat_review_state WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    assert state is None

    # Nothing delivered.
    notifs = ingested_conn.execute(
        "SELECT COUNT(*) AS n FROM notifications WHERE run_id = ?", (outcome.run_id,)
    ).fetchone()["n"]
    assert notifs == 0

    # A full trace was still recorded.
    assert _trace(ingested_conn, outcome.run_id, chat_id)["final_action"] == "actionable"


def test_dry_run_is_deterministic(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    _monitor(ingested_conn, "chat-class-4a")
    _monitor(ingested_conn, "chat-school-parents")
    config = _config(tmp_path)

    first = scan(ingested_conn, config, mode="dry_run", classifier=StubClassifier())
    second = scan(ingested_conn, config, mode="dry_run", classifier=StubClassifier())

    assert first.digest is not None and second.digest is not None
    assert first.chats_with_delta == second.chats_with_delta
    assert first.actionable == second.actionable
    assert [i.chat for i in first.digest.items] == [i.chat for i in second.digest.items]


def test_dry_run_days_windows_the_replay(conn: sqlite3.Connection, tmp_path: Path) -> None:
    chat_id = store.upsert_chat(conn, ChatRecord(source_chat_id="c-win", display_name="Window"))
    store.set_chat_status(conn, chat_id, "monitored")
    now = datetime.now(UTC)
    store.insert_message(
        conn, chat_id,
        MessageRecord(
            source_message_id="old",
            message_timestamp=(now - timedelta(days=10)).isoformat(),
            text="hello",
        ),
    )
    store.insert_message(
        conn, chat_id,
        MessageRecord(
            source_message_id="recent",
            message_timestamp=(now - timedelta(hours=2)).isoformat(),
            text="hello",
        ),
    )

    outcome = scan(conn, _config(tmp_path), mode="dry_run", days=1, classifier=StubClassifier())

    ids = json.loads(_trace(conn, outcome.run_id, chat_id)["input_message_ids_json"])
    assert ids == ["recent"]  # the 10-day-old message is outside the window


# --- audit trace -----------------------------------------------------------

def test_trace_captures_prompt_and_raw_response(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    chat_id = _monitor(ingested_conn, "chat-class-4a")
    fake = _FakeTraced(_ACTIONABLE_JSON)

    outcome = scan(
        ingested_conn, _config(tmp_path), mode="live",
        connector=FixtureConnector(), classifier=fake,
    )

    assert fake.calls == 1
    assert outcome.stage2_llm_calls == 1
    row = _trace(ingested_conn, outcome.run_id, chat_id)
    assert row["stage1_passed"] == 1
    assert json.loads(row["stage1_roots_json"])  # Stage-1 evidence recorded
    assert row["llm_system_prompt"] == "SYSTEM-PROMPT"
    assert row["llm_user_prompt"].startswith("USER-PROMPT for Class 4A Group")
    assert "<think>" in row["llm_raw_response"]
    assert row["final_action"] == "actionable"
    assert json.loads(row["parsed_result_json"])["action_required"] is True
    assert "Class 4A Group" in row["telegram_text"]


def test_stage1_noise_skips_the_llm(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    chat_id = _monitor(ingested_conn, "chat-building")  # only small talk, no roots
    fake = _FakeTraced(_ACTIONABLE_JSON)

    outcome = scan(
        ingested_conn, _config(tmp_path), mode="live",
        connector=FixtureConnector(), classifier=fake,
    )

    assert fake.calls == 0  # Stage 1 gated the LLM
    assert outcome.stage1_passed == 0
    assert outcome.stage2_llm_calls == 0
    row = _trace(ingested_conn, outcome.run_id, chat_id)
    assert row["final_action"] == "not_actionable"
    assert row["llm_called"] == 0
    # Cursor still advances: the delta was processed (just not actionable).
    assert ingested_conn.execute(
        "SELECT 1 FROM chat_review_state WHERE chat_id = ?", (chat_id,)
    ).fetchone() is not None


def test_contract_error_traces_and_does_not_advance_cursor(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    chat_id = _monitor(ingested_conn, "chat-class-4a")
    config = _config(tmp_path)

    bad = scan(
        ingested_conn, config, mode="live",
        connector=FixtureConnector(), classifier=_FakeTraced("{ not valid json"),
    )

    assert bad.errors and bad.errors[0][0] == chat_id
    row = _trace(ingested_conn, bad.run_id, chat_id)
    assert row["final_action"] == "contract_error"
    assert row["parsed_result_json"] is None
    assert row["error"]
    # No analysis item, no cursor advance -> the same delta is retried next run.
    assert ingested_conn.execute(
        "SELECT COUNT(*) AS n FROM analysis_items WHERE run_id = ?", (bad.run_id,)
    ).fetchone()["n"] == 0
    assert ingested_conn.execute(
        "SELECT 1 FROM chat_review_state WHERE chat_id = ?", (chat_id,)
    ).fetchone() is None

    recovered = scan(
        ingested_conn, config, mode="live",
        connector=FixtureConnector(), classifier=StubClassifier(),
    )
    assert recovered.chats_with_delta == 1
    assert recovered.actionable == 1
