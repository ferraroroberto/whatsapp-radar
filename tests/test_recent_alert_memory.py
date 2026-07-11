"""Short-term alert memory (#66): repeated to-dos must not be re-alerted forever.

All offline: the fixture connector, deterministic stubs, and a recording
classifier double stand in for any network. The suppress-vs-escalate *verdict*
is the LLM's; these tests prove the *mechanism* — that the right already-surfaced
context (summary + deadline) is queried, survives an intervening noise delta, and
reaches Stage 2 — which is what the offline suite can guarantee.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from src.analysis.classifier import ClassificationOutcome
from src.analysis.pipeline import scan
from src.analysis.review import format_recent_alerts, recent_alert_context
from src.config import Config, HubConfig, TelegramConfig
from src.connector.fixture import FixtureConnector
from src.db import store
from src.models import ChatRecord, StoredMessage
from tests.helpers import append_message, chat_id_by_source

_NOW = datetime(2026, 6, 9, tzinfo=UTC)

_ACTIONABLE_JSON = json.dumps(
    {
        "action_required": True,
        "priority": "high",
        "summary": "Pay the trip fee",
        "suggested_next_action": "Pay it",
        "deadline": "Friday",
        "confidence": 0.9,
        "evidence_message_ids": ["x"],
    }
)


def _config(tmp_path: Path) -> Config:
    return Config(
        db_path=tmp_path / "unused.sqlite3",
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="claude_sonnet"),
        notifier="none",
        telegram=TelegramConfig(bot_token="t", chat_id="c"),
        linked_device_dir=tmp_path / "ld",
    )


def _run_at(conn: sqlite3.Connection, started_at: str) -> int:
    cur = conn.execute(
        "INSERT INTO review_runs (started_at, status, mode) VALUES (?, 'completed', 'live')",
        (started_at,),
    )
    conn.commit()
    return int(cur.lastrowid)


def _actionable(
    conn: sqlite3.Connection,
    run_id: int,
    chat_id: int,
    summary: str | None,
    *,
    deadline: str | None = None,
    action_required: bool = True,
) -> None:
    store.insert_analysis_item(
        conn,
        run_id,
        chat_id,
        action_required=action_required,
        priority="high" if action_required else None,
        summary=summary,
        suggested_next_action="do it" if action_required else None,
        deadline=deadline,
        confidence=0.9,
        evidence_message_ids_json=json.dumps([]),
    )


class _RecordingClassifier:
    """A Stage-2 double that records the prior context it is handed each call."""

    def __init__(self, raw_output: str) -> None:
        self.calls = 0
        self.priors: list[str | None] = []
        self._raw = raw_output

    def classify_traced(
        self,
        chat_display_name: str,
        delta: list[StoredMessage],
        prior_context: str | None,
        *,
        source: str = "whatsapp",
    ) -> ClassificationOutcome:
        self.calls += 1
        self.priors.append(prior_context)
        return ClassificationOutcome(
            raw_output=self._raw,
            llm_called=True,
            system_prompt="S",
            user_prompt="U",
            raw_response=self._raw,
        )


# --- store query: window, family scope, exclusions -------------------------

def test_recent_actionable_items_windows_scopes_and_excludes(conn: sqlite3.Connection) -> None:
    head = store.upsert_chat(conn, ChatRecord(source_chat_id="head", display_name="Head"))
    child = store.upsert_chat(conn, ChatRecord(source_chat_id="child", display_name="Child"))
    store.link_chats(conn, child, head)

    _actionable(conn, _run_at(conn, "2026-06-05T09:00:00+00:00"), head, "Pay the trip fee",
                deadline="Friday")
    _actionable(conn, _run_at(conn, "2026-06-07T09:00:00+00:00"), child, "Bring swimsuit")
    # Older than the 7-day window → excluded.
    _actionable(conn, _run_at(conn, "2026-05-01T09:00:00+00:00"), head, "Ancient task")
    # Not actionable → excluded.
    _actionable(conn, _run_at(conn, "2026-06-09T08:00:00+00:00"), head, None,
                action_required=False)
    # Actionable but no summary (nothing to re-surface) → excluded.
    _actionable(conn, _run_at(conn, "2026-06-09T08:30:00+00:00"), head, None)
    excluded_run = _run_at(conn, "2026-06-08T09:00:00+00:00")
    _actionable(conn, excluded_run, head, "Should be dropped by exclude_run_id")

    items = store.recent_actionable_items(
        conn, head, since_days=7, now=_NOW, exclude_run_id=excluded_run
    )

    # In-window, family-wide (head + child), chronological, exclusions honoured.
    assert [it["summary"] for it in items] == ["Pay the trip fee", "Bring swimsuit"]
    assert items[0]["deadline"] == "Friday"


# --- renderer --------------------------------------------------------------

def test_format_recent_alerts_empty_is_none() -> None:
    assert format_recent_alerts([]) is None


def test_format_recent_alerts_renders_date_summary_and_deadline(
    conn: sqlite3.Connection,
) -> None:
    chat = store.upsert_chat(conn, ChatRecord(source_chat_id="c", display_name="C"))
    _actionable(conn, _run_at(conn, "2026-06-05T09:00:00+00:00"), chat, "Pay the trip fee",
                deadline="Friday")

    block = recent_alert_context(conn, chat, since_days=7, now=_NOW)

    assert block is not None
    assert "do NOT raise these again" in block
    assert "[2026-06-05] Pay the trip fee" in block
    assert "deadline: Friday" in block


# --- integration: memory survives a noise delta (the core regression) ------

def test_prior_alert_reaches_stage2_after_intervening_noise_delta(
    ingested_conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """An actionable alert must still be in Stage-2's memory a run later, even
    after a noise delta — the exact case the old null-wiped last_summary lost.
    """
    chat_id = chat_id_by_source(ingested_conn, "chat-class-4a")
    store.set_chat_status(ingested_conn, chat_id, "monitored")
    config = _config(tmp_path)
    recording = _RecordingClassifier(_ACTIONABLE_JSON)

    # Run 1: the chat's backlog is actionable → an alert is recorded.
    scan(ingested_conn, config, mode="live",
         connector=FixtureConnector(), classifier=recording)
    assert recording.calls == 1
    assert recording.priors[0] is None  # no history yet

    # A pure-noise delta: Stage 1 gates it, the cursor advances with a null
    # summary (this is what used to wipe the memory). No LLM call.
    append_message(ingested_conn, "chat-class-4a", "noise-1", "great, see you all",
                   timestamp="2026-06-11T10:00:00+00:00")
    scan(ingested_conn, config, mode="live",
         connector=FixtureConnector(), classifier=recording)
    assert recording.calls == 1  # noise did not reach Stage 2

    # The lazy parent repeats the same ask a couple of days later.
    append_message(ingested_conn, "chat-class-4a", "repeat-1", "please pay the fee",
                   timestamp="2026-06-12T10:00:00+00:00")
    scan(ingested_conn, config, mode="live",
         connector=FixtureConnector(), classifier=recording)

    assert recording.calls == 2
    prior = recording.priors[1]
    assert prior is not None
    # The run-1 alert survived the intervening noise delta...
    assert "Pay the trip fee" in prior
    # ...with its deadline, so the model can escalate rather than blindly suppress.
    assert "deadline: Friday" in prior
