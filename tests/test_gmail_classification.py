"""Source-aware Gmail rules, prompts, survey boundary, and audit evidence (#60)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from src.analysis.classifier import HubClassifier
from src.analysis.gmail_survey import parse_survey_proposal, run_gmail_survey
from src.analysis.keywords import has_actionable_signal
from src.analysis.pipeline import scan
from src.config import (
    Config,
    GmailConfig,
    GmailSender,
    HubConfig,
    TelegramConfig,
)
from src.db import store
from src.models import ChatRecord, MessageRecord, StoredMessage


def _stored(text: str) -> StoredMessage:
    return StoredMessage(
        id=1,
        chat_id=1,
        source_message_id="generic-1",
        message_timestamp="2026-07-10T08:00:00+00:00",
        text=text,
        sender_label="Example Sender",
        message_type="email",
    )


def _config(tmp_path: Path, *, gmail: GmailConfig | None = None) -> Config:
    return Config(
        db_path=tmp_path / "test.sqlite3",
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="stub"),
        notifier="none",
        telegram=TelegramConfig("", ""),
        linked_device_dir=tmp_path / "linked",
        sources=("gmail",),
        gmail=gmail or GmailConfig(),
    )


def test_stage1_selects_rules_by_source() -> None:
    gmail = has_actionable_signal([_stored("Your invoice is ready")], "gmail")
    whatsapp = has_actionable_signal([_stored("Your invoice is ready")], "whatsapp")

    assert gmail.matched is True
    assert gmail.roots == ("invoice",)
    assert gmail.buckets == ("payment",)
    assert whatsapp.matched is False


def test_stage2_prompt_names_gmail_and_whatsapp_sources() -> None:
    hub = HubClassifier(HubConfig(base_url="http://x", model="stub"))

    gmail = hub._build_user_prompt(
        "Example Updates", [_stored("Subject: Schedule\n\nChanged")], None, source="gmail"
    )
    whatsapp = hub._build_user_prompt(
        "Family Group", [_stored("Please confirm")], None, source="whatsapp"
    )

    assert gmail.startswith("Source: Gmail\nChannel: Example Updates")
    assert "New emails" in gmail
    assert "WhatsApp chat" not in gmail
    assert whatsapp.startswith("Source: WhatsApp\nChannel: Family Group")
    assert "New messages" in whatsapp


class _MustNotCall:
    def classify_traced(
        self,
        chat_display_name: str,
        delta: list[StoredMessage],
        prior_context: str | None,
        *,
        source: str = "whatsapp",
    ) -> Any:
        raise AssertionError("Stage-1-rejected Gmail must not call the LLM")


def test_gmail_stage1_rejection_skips_llm_and_records_source(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    chat_id = store.upsert_chat(
        conn,
        ChatRecord(
            source_chat_id="sender:alerts@example.test",
            display_name="Example Updates",
            chat_type="email",
            source="gmail",
        ),
    )
    store.insert_messages(
        conn,
        chat_id,
        [
            MessageRecord(
                source_message_id="generic-1",
                message_timestamp="2026-07-10T08:00:00+00:00",
                text="Weekly news and photos",
                sender_label="Example Sender",
                message_type="email",
            )
        ],
    )
    store.set_chat_status(conn, chat_id, "monitored")

    outcome = scan(
        conn,
        _config(tmp_path),
        mode="dry_run",
        classifier=_MustNotCall(),
    )
    trace = store.traces_for_run(conn, outcome.run_id)[0]

    assert outcome.stage2_llm_calls == 0
    assert trace["source"] == "gmail"
    assert json.loads(trace["stage1_buckets_json"]) == []
    assert trace["llm_user_prompt"] is None


class _SurveyClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def get_profile(self) -> dict[str, Any]:
        return {"emailAddress": "o***@example.test"}

    def list_labels(self) -> list[dict[str, Any]]:
        return []

    def list_message_ids(
        self, *, query: str, label_ids: list[str] | None = None
    ) -> list[str]:
        self.queries.append(query)
        return ["message-1"]

    def get_message(
        self, message_id: str, *, metadata_only: bool = False
    ) -> dict[str, Any]:
        return {
            "id": message_id,
            "threadId": "thread-1",
            "internalDate": "1783670400000",
            "payload": {
                "headers": [
                    {"name": "From", "value": "Example Sender <alerts@example.test>"},
                    {"name": "Subject", "value": "Generic reminder"},
                ],
                "mimeType": "text/plain",
                "body": {"data": "UGxlYXNlIGNvbmZpcm0="},
            },
        }

    def close(self) -> None:
        return None


def test_gmail_survey_defaults_to_60_days_and_reports_before_llm(
    tmp_path: Path, monkeypatch: Any
) -> None:
    client = _SurveyClient()
    lines: list[str] = []
    proposals: list[Any] = []
    config = _config(
        tmp_path,
        gmail=GmailConfig(
            senders=(GmailSender("alerts@example.test", "Example Sender"),)
        ),
    )

    monkeypatch.setattr(
        "src.analysis.gmail_survey.build_gmail_read_client", lambda _config: client
    )

    def fake_hub(_config: Config, _samples: list[Any]) -> str:
        assert lines and lines[0].startswith("Scope:")
        return json.dumps(
            {
                "buckets": [
                    {
                        "name": "response",
                        "description": "A reply or confirmation is required.",
                        "roots": ["confirm"],
                    }
                ]
            }
        )

    monkeypatch.setattr("src.analysis.gmail_survey._call_hub", fake_hub)
    monkeypatch.setattr(
        "src.analysis.gmail_survey.write_survey_assets", proposals.append
    )

    scope = run_gmail_survey(config, max_messages=1, progress=lines.append)

    assert scope.message_count == 1
    assert client.queries
    assert all("newer_than:60d" in query for query in client.queries)
    assert len(proposals) == 1


def test_survey_rejects_identifiers_before_writing() -> None:
    raw = json.dumps(
        {
            "buckets": [
                {
                    "name": "response",
                    "description": "Messages from Example Sender need a reply.",
                    "roots": ["confirm"],
                }
            ]
        }
    )
    try:
        parse_survey_proposal(raw, forbidden_fragments={"example sender"})
    except ValueError as exc:
        assert "identifier" in str(exc)
    else:
        raise AssertionError("personal identifiers must fail closed")
