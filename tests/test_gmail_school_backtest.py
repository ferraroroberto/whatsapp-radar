"""Historical Gmail school backtest script — read-only guarantee (#216)."""

from __future__ import annotations

import argparse
import base64
import json
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from gmail_readonly import GmailMailbox, GmailSender

from scripts.gmail_school_backtest import (
    _dated_search,
    _parse_date,
    resolve_whitelist,
    run_backtest,
)
from src.analysis.classifier import ClassificationOutcome, StubClassifier
from src.config import Config, GmailConfig, HubConfig, TelegramConfig
from src.config import GmailSender as ConfigGmailSender


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def _raw_message(
    message_id: str, *, sender: str, subject: str, body: str, sent_at: str
) -> dict[str, Any]:
    return {
        "id": message_id,
        "threadId": f"thread-{message_id}",
        "internalDate": sent_at,
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "Subject", "value": subject},
            ],
            "body": {"data": _encoded(body)},
        },
    }


class _FakeClient:
    """Minimal fake Gmail client — records the query strings it was asked to run."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self._messages = {message["id"]: message for message in messages}
        self.queries: list[str] = []

    def get_profile(self) -> dict[str, Any]:
        return {"emailAddress": "family@example.test"}

    def list_labels(self) -> list[dict[str, Any]]:
        return []

    def list_message_ids(
        self, *, query: str, label_ids: list[str] | None = None
    ) -> list[str]:
        self.queries.append(query)
        return list(self._messages.keys())

    def get_message(
        self, message_id: str, *, metadata_only: bool = False
    ) -> dict[str, Any]:
        return self._messages[message_id]

    def close(self) -> None:
        return None


class _FlakyClassifier:
    """Fails contract validation on its first call, succeeds after (#216 resilience)."""

    def __init__(self) -> None:
        self.calls = 0

    def classify_traced(
        self,
        chat_display_name: str,
        delta: list[Any],
        prior_context: str | None,
        *,
        source: str = "whatsapp",
    ) -> ClassificationOutcome:
        self.calls += 1
        if self.calls == 1:
            return ClassificationOutcome(raw_output="not valid json")
        return ClassificationOutcome(
            raw_output=json.dumps(
                {"action_required": True, "confidence": 0.5, "evidence_message_ids": []}
            )
        )


def test_parse_date_rejects_bad_format() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_date("09/01/2025")


def test_parse_date_accepts_iso_format() -> None:
    assert _parse_date("2025-09-01") == date(2025, 9, 1)


def _config(*, gmail: GmailConfig | None = None) -> Config:
    return Config(
        db_path=Path("unused.sqlite3"),
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="stub"),
        notifier="none",
        telegram=TelegramConfig("", ""),
        linked_device_dir=Path("unused"),
        sources=("gmail",),
        gmail=gmail or GmailConfig(),
    )


def test_resolve_whitelist_prefers_cli_overrides() -> None:
    config = _config(
        gmail=GmailConfig(senders=(ConfigGmailSender("configured@example.test", "Configured"),))
    )

    senders, labels = resolve_whitelist(
        config, sender_overrides=["override@example.test"], label_overrides=[]
    )

    assert senders == (
        GmailSender(address="override@example.test", display_name="override@example.test"),
    )
    assert labels == ()


def test_resolve_whitelist_falls_back_to_configured_whitelist() -> None:
    config = _config(
        gmail=GmailConfig(senders=(ConfigGmailSender("configured@example.test", "Configured"),))
    )

    senders, _labels = resolve_whitelist(config, sender_overrides=[], label_overrides=[])

    assert senders == (GmailSender(address="configured@example.test", display_name="Configured"),)


def test_resolve_whitelist_rejects_empty_whitelist() -> None:
    with pytest.raises(ValueError, match="no sender/label whitelist"):
        resolve_whitelist(_config(), sender_overrides=[], label_overrides=[])


def test_dated_search_appends_after_before_operators() -> None:
    client = _FakeClient([])
    mailbox = GmailMailbox(client)
    (source,) = mailbox.resolve_sources(
        senders=(GmailSender("school@example.test", "School"),)
    )

    dated = _dated_search(source.search, after=date(2025, 9, 1), before=date(2025, 10, 1))

    assert dated.query == "from:school@example.test after:2025/09/01 before:2025/10/01"
    assert dated.lookback_days is None


def test_run_backtest_rejects_after_not_before_before() -> None:
    mailbox = GmailMailbox(_FakeClient([]))
    with pytest.raises(ValueError, match="--after must be earlier"):
        run_backtest(
            mailbox,
            StubClassifier(),
            after=date(2025, 10, 1),
            before=date(2025, 9, 1),
            senders=(GmailSender("school@example.test", "School"),),
            labels=(),
        )


def test_run_backtest_classifies_each_email_via_injected_classifier() -> None:
    messages = [
        _raw_message(
            "m1",
            sender="School Office <school@example.test>",
            subject="Field trip",
            body="Please sign and return the permission form by Friday.",
            sent_at="1756713600000",
        ),
        _raw_message(
            "m2",
            sender="School Office <school@example.test>",
            subject="Newsletter",
            body="Nothing to action this week.",
            sent_at="1756800000000",
        ),
    ]
    mailbox = GmailMailbox(_FakeClient(messages))
    lines: list[str] = []

    records = run_backtest(
        mailbox,
        StubClassifier(),
        after=date(2025, 9, 1),
        before=date(2025, 10, 1),
        senders=(GmailSender("school@example.test", "School"),),
        labels=(),
        progress=lines.append,
    )

    assert len(records) == 2
    assert {record["message_id"] for record in records} == {"m1", "m2"}
    assert records[0]["sender"] == "school@example.test"
    assert any("Scope: 2 email(s)" in line for line in lines)


def test_run_backtest_skips_contract_errors_and_continues() -> None:
    messages = [
        _raw_message(
            "m1",
            sender="School Office <school@example.test>",
            subject="A",
            body="Body A",
            sent_at="1756713600000",
        ),
        _raw_message(
            "m2",
            sender="School Office <school@example.test>",
            subject="B",
            body="Body B",
            sent_at="1756800000000",
        ),
    ]
    mailbox = GmailMailbox(_FakeClient(messages))
    classifier = _FlakyClassifier()
    lines: list[str] = []

    records = run_backtest(
        mailbox,
        classifier,
        after=date(2025, 9, 1),
        before=date(2025, 10, 1),
        senders=(GmailSender("school@example.test", "School"),),
        labels=(),
        progress=lines.append,
    )

    assert classifier.calls == 2
    assert len(records) == 1
    assert any("classification failed" in line for line in lines)


def test_run_backtest_never_opens_sqlite(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-only guarantee: no code path in run_backtest may open the local DB."""

    def _forbidden_connect(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("run_backtest must never call sqlite3.connect")

    monkeypatch.setattr(sqlite3, "connect", _forbidden_connect)

    mailbox = GmailMailbox(
        _FakeClient(
            [
                _raw_message(
                    "m1",
                    sender="School Office <school@example.test>",
                    subject="A",
                    body="Body A",
                    sent_at="1756713600000",
                )
            ]
        )
    )

    records = run_backtest(
        mailbox,
        StubClassifier(),
        after=date(2025, 9, 1),
        before=date(2025, 10, 1),
        senders=(GmailSender("school@example.test", "School"),),
        labels=(),
    )

    assert len(records) == 1
