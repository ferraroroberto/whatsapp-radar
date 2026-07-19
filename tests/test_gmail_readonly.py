"""Portable offline contract tests for the root-level Gmail component."""

from __future__ import annotations

import ast
import base64
from pathlib import Path
from typing import Any

import pytest
from gmail_readonly import (
    GMAIL_READONLY_SCOPE,
    GmailLabel,
    GmailMailbox,
    GmailReadError,
    GmailSearch,
    GmailSender,
    GoogleGmailReadClient,
    build_google_read_client,
    masked_email_address,
)
from gmail_readonly.oauth import authorize


def _encoded(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


class _FakeClient:
    def __init__(self) -> None:
        self.queries: list[tuple[str, list[str] | None]] = []
        self.metadata_modes: list[bool] = []
        self.closed = False
        self.messages = {
            "newer": {
                "id": "newer",
                "threadId": "thread-2",
                "internalDate": "1783681200000",
                "labelIds": ["LBL_ACT"],
                "payload": {
                    "mimeType": "multipart/mixed",
                    "headers": [
                        {"name": "From", "value": "School Office <school@example.com>"},
                        {"name": "To", "value": "family@example.net"},
                        {"name": "Subject", "value": "Trip form"},
                        {"name": "Message-ID", "value": "<newer@example.com>"},
                    ],
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "filename": "",
                            "body": {"data": _encoded("Return by Friday.")},
                        },
                        {
                            "mimeType": "application/pdf",
                            "filename": "private.pdf",
                            "body": {"data": _encoded("must never be decoded")},
                        },
                    ],
                },
            },
            "older": {
                "id": "older",
                "threadId": "thread-1",
                "internalDate": "1783594800000",
                "payload": {
                    "mimeType": "text/html",
                    "headers": [
                        {"name": "From", "value": "Coach <coach@example.com>"},
                        {"name": "Subject", "value": "Practice"},
                    ],
                    "body": {"data": _encoded("<p>Bring <strong>water</strong>.</p>")},
                },
            },
        }

    def get_profile(self) -> dict[str, Any]:
        return {
            "emailAddress": "family@example.net",
            "messagesTotal": 12,
            "threadsTotal": 8,
        }

    def list_labels(self) -> list[dict[str, Any]]:
        return [
            {"id": "LBL_ACT", "name": "Family/Activities"},
            {"id": "LBL_URG", "name": "Family/Urgent"},
        ]

    def list_message_ids(
        self,
        *,
        query: str,
        label_ids: list[str] | None = None,
    ) -> list[str]:
        self.queries.append((query, label_ids))
        return ["newer", "older"]

    def get_message(
        self,
        message_id: str,
        *,
        metadata_only: bool = False,
    ) -> dict[str, Any]:
        self.metadata_modes.append(metadata_only)
        return self.messages[message_id]

    def close(self) -> None:
        self.closed = True


def test_component_imports_no_application_modules() -> None:
    component_root = Path(__file__).parents[1] / "gmail_readonly"
    imported_modules: set[str] = set()
    for path in component_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
    assert not any(
        module == prefix or module.startswith(f"{prefix}.")
        for module in imported_modules
        for prefix in ("src", "app", "scripts")
    )


def test_whitelist_precedence_label_validation_and_bounded_queries() -> None:
    mailbox = GmailMailbox(_FakeClient())
    sources = mailbox.resolve_sources(
        senders=(GmailSender("SCHOOL@example.com", "School"),),
        labels=(
            GmailLabel("Family/Activities", "Activities"),
            GmailLabel("Family/Urgent", "Urgent"),
        ),
        lookback_days=60,
    )

    assert sources[0].source_id == "sender:school@example.com"
    assert sources[0].search.api_query() == "from:school@example.com newer_than:60d"
    assert sources[1].search.api_query() == "-from:school@example.com newer_than:60d"
    assert sources[1].search.label_ids == ("LBL_ACT",)
    assert sources[2].search.api_query() == (
        '-from:school@example.com -label:"Family/Activities" newer_than:60d'
    )
    with pytest.raises(ValueError, match="not found"):
        mailbox.resolve_sources(labels=(GmailLabel("Missing", "Missing"),))
    with pytest.raises(ValueError, match="duplicate sender"):
        mailbox.resolve_sources(
            senders=(
                GmailSender("same@example.com", "One"),
                GmailSender("same@example.com", "Two"),
            )
        )


def test_count_metadata_and_normalized_search_modes() -> None:
    client = _FakeClient()
    mailbox = GmailMailbox(client)
    search = GmailSearch(query="from:school@example.com", lookback_days=60)

    assert mailbox.count(search) == 2
    assert client.metadata_modes == []
    metadata = mailbox.metadata(search)
    assert client.metadata_modes == [True, True]
    assert metadata[0].message_id == "older"
    client.metadata_modes.clear()
    messages = mailbox.messages(search)

    assert client.queries == [
        ("from:school@example.com newer_than:60d", None),
        ("from:school@example.com newer_than:60d", None),
        ("from:school@example.com newer_than:60d", None),
    ]
    assert client.metadata_modes == [False, False]
    assert [message.message_id for message in messages] == ["older", "newer"]
    assert messages[0].text == "Subject: Practice\n\nBring water."
    assert messages[1].text == "Subject: Trip form\n\nReturn by Friday."
    assert "must never be decoded" not in str(messages)
    assert messages[1].thread_id == "thread-2"
    assert messages[1].headers["message-id"] == "<newer@example.com>"


def test_discover_senders_groups_recent_metadata_by_address() -> None:
    client = _FakeClient()
    mailbox = GmailMailbox(client)

    discovered = mailbox.discover_senders(days=30, limit=100)

    # One entry per distinct From address, friendliest name kept, newest first.
    assert [(sender.address, sender.display_name) for sender in discovered] == [
        ("school@example.com", "School Office"),
        ("coach@example.com", "Coach"),
    ]
    assert all(sender.message_count == 1 for sender in discovered)
    # Discovery scans a bounded recent window and reads metadata only — never bodies.
    assert client.queries[-1][0] == "newer_than:30d"
    assert client.metadata_modes == [True, True]
    with pytest.raises(ValueError, match="at least 1"):
        mailbox.discover_senders(days=0, limit=100)
    with pytest.raises(ValueError, match="at least 1"):
        mailbox.discover_senders(days=30, limit=0)


def test_profile_mask_safe_failures_and_cleanup() -> None:
    client = _FakeClient()
    mailbox = GmailMailbox(client)
    profile = mailbox.profile()

    assert profile.masked_email_address == "f***@example.net"
    assert profile.messages_total == 12
    assert masked_email_address("invalid") == "***"
    mailbox.close()
    assert client.closed is True

    class _Response:
        status = 429

    class _FailingClient(_FakeClient):
        def list_message_ids(
            self,
            *,
            query: str,
            label_ids: list[str] | None = None,
        ) -> list[str]:
            error = RuntimeError("private query and message content")
            error.resp = _Response()  # type: ignore[attr-defined]
            raise error

    with pytest.raises(GmailReadError, match="quota exceeded") as caught:
        GmailMailbox(_FailingClient()).count(GmailSearch(query="private"))
    assert "private" not in str(caught.value)


def test_message_retrieval_limit_bounds_full_fetches() -> None:
    client = _FakeClient()
    mailbox = GmailMailbox(client)

    messages = mailbox.messages(GmailSearch(query="in:inbox"), limit=1)

    assert [message.message_id for message in messages] == ["newer"]
    assert client.metadata_modes == [False]
    with pytest.raises(ValueError, match="limit must be at least 1"):
        mailbox.messages(GmailSearch(query="in:inbox"), limit=0)


def test_query_validation_rejects_unbounded_control_characters() -> None:
    with pytest.raises(ValueError, match="single line"):
        GmailSearch(query="from:a@example.com\nsubject:private").api_query()
    with pytest.raises(ValueError, match="at least 1"):
        GmailSearch(lookback_days=0).api_query()


class _Request:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def execute(self) -> dict[str, Any]:
        return self.payload


class _MessagesResource:
    def __init__(self) -> None:
        self.list_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def list(self, **kwargs: Any) -> _Request:
        self.list_calls.append(kwargs)
        if len(self.list_calls) == 1:
            return _Request({"messages": [{"id": "one"}], "nextPageToken": "next"})
        return _Request({"messages": [{"id": "two"}]})

    def get(self, **kwargs: Any) -> _Request:
        self.get_calls.append(kwargs)
        return _Request({"id": kwargs["id"]})


class _UsersResource:
    def __init__(self) -> None:
        self.message_resource = _MessagesResource()

    def messages(self) -> _MessagesResource:
        return self.message_resource


class _Service:
    def __init__(self) -> None:
        self.user_resource = _UsersResource()

    def users(self) -> _UsersResource:
        return self.user_resource


def test_google_client_paginates_and_requests_metadata_only() -> None:
    service = _Service()
    client = GoogleGmailReadClient(service)

    assert client.list_message_ids(query="newer_than:60d", label_ids=["LBL"]) == [
        "one",
        "two",
    ]
    calls = service.user_resource.message_resource.list_calls
    assert calls[0]["includeSpamTrash"] is False
    assert calls[1]["pageToken"] == "next"
    client.get_message("one", metadata_only=True)
    get_call = service.user_resource.message_resource.get_calls[0]
    assert get_call["format"] == "metadata"
    assert "Subject" in get_call["metadataHeaders"]


def test_token_absence_refresh_and_exact_scope(tmp_path: Path) -> None:
    token_path = tmp_path / "token.json"
    with pytest.raises(FileNotFoundError, match="OAuth token missing"):
        build_google_read_client(token_path)
    token_path.write_text("{}", encoding="utf-8")
    observed: dict[str, Any] = {}

    class _Credentials:
        expired = True
        refresh_token = "present"
        valid = True

        def refresh(self, request: object) -> None:
            observed["request"] = request

        def to_json(self) -> str:
            return '{"refreshed": true}'

    credentials = _Credentials()

    def load_credentials(path: str, scopes: list[str]) -> _Credentials:
        observed["token_path"] = path
        observed["scopes"] = scopes
        return credentials

    service = _Service()
    client = build_google_read_client(
        token_path,
        credential_loader=load_credentials,
        request_factory=lambda: "request",
        service_builder=lambda *args, **kwargs: service,
    )

    assert isinstance(client, GoogleGmailReadClient)
    assert observed["scopes"] == [GMAIL_READONLY_SCOPE]
    assert observed["request"] == "request"
    assert token_path.read_text(encoding="utf-8") == '{"refreshed": true}'
    assert not token_path.with_suffix(".json.tmp").exists()


def test_oauth_uses_explicit_paths_readonly_scope_and_atomic_write(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"
    token_path = tmp_path / "auth" / "token.json"
    credentials_path.write_text("{}", encoding="utf-8")
    observed: dict[str, Any] = {}

    class _Credentials:
        refresh_token = "present"

        def to_json(self) -> str:
            return '{"token": "secret"}'

    class _Flow:
        def run_local_server(self, **kwargs: Any) -> _Credentials:
            observed["server"] = kwargs
            return _Credentials()

    def load_flow(path: str, scopes: list[str]) -> _Flow:
        observed["credentials_path"] = path
        observed["scopes"] = scopes
        return _Flow()

    authorize(
        credentials_path=credentials_path,
        token_path=token_path,
        host="127.0.0.1",
        port=8765,
        open_browser=False,
        flow_loader=load_flow,
    )

    assert observed["scopes"] == [GMAIL_READONLY_SCOPE]
    assert observed["server"]["open_browser"] is False
    assert observed["server"]["port"] == 8765
    assert token_path.read_text(encoding="utf-8") == '{"token": "secret"}'


def test_public_core_exposes_no_write_operations() -> None:
    forbidden = {
        "send",
        "draft",
        "modify",
        "archive",
        "trash",
        "delete",
        "insert",
        "label",
    }
    assert forbidden.isdisjoint(set(dir(GmailMailbox)))
    assert forbidden.isdisjoint(set(dir(GoogleGmailReadClient)))
