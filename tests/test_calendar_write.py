"""Portable offline contract tests for the write-scope Calendar component (#217)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from calendar_write import (
    CALENDAR_WRITE_SCOPE,
    GoogleCalendarWriteClient,
    build_google_calendar_write_client,
)
from calendar_write.oauth import authorize


def test_write_scope_is_narrowest_events_scope() -> None:
    """Regression guard for the issue's "never widen the scope" constraint."""
    assert CALENDAR_WRITE_SCOPE == "https://www.googleapis.com/auth/calendar.events"


class _EventsResource:
    def __init__(self) -> None:
        self.insert_calls: list[dict[str, Any]] = []
        self.delete_calls: list[dict[str, Any]] = []

    def insert(self, *, calendarId: str, body: dict[str, Any]) -> _Execute:
        self.insert_calls.append({"calendarId": calendarId, "body": body})
        return _Execute({"id": "created-event-1", **body})

    def delete(self, *, calendarId: str, eventId: str) -> _Execute:
        self.delete_calls.append({"calendarId": calendarId, "eventId": eventId})
        return _Execute(None)


class _Execute:
    def __init__(self, result: Any) -> None:
        self._result = result

    def execute(self) -> Any:
        return self._result


class _Service:
    def __init__(self) -> None:
        self.events_resource = _EventsResource()

    def events(self) -> _EventsResource:
        return self.events_resource


def test_google_client_inserts_and_deletes_events() -> None:
    service = _Service()
    client = GoogleCalendarWriteClient(service)

    created = client.insert_event(
        calendar_id="family@example.test", event={"summary": "Trip form due"}
    )
    assert created["id"] == "created-event-1"
    assert service.events_resource.insert_calls[0]["calendarId"] == "family@example.test"
    assert service.events_resource.insert_calls[0]["body"] == {"summary": "Trip form due"}

    client.delete_event(calendar_id="family@example.test", event_id="created-event-1")
    assert service.events_resource.delete_calls[0] == {
        "calendarId": "family@example.test",
        "eventId": "created-event-1",
    }


def test_token_absence_refresh_and_exact_write_scope(tmp_path: Path) -> None:
    token_path = tmp_path / "write_token.json"
    with pytest.raises(FileNotFoundError, match="write OAuth token missing"):
        build_google_calendar_write_client(token_path)
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
    client = build_google_calendar_write_client(
        token_path,
        credential_loader=load_credentials,
        request_factory=lambda: "request",
        service_builder=lambda *args, **kwargs: service,
    )

    assert isinstance(client, GoogleCalendarWriteClient)
    assert observed["scopes"] == [CALENDAR_WRITE_SCOPE]
    assert observed["request"] == "request"
    assert token_path.read_text(encoding="utf-8") == '{"refreshed": true}'
    assert not token_path.with_suffix(".json.tmp").exists()


def test_oauth_uses_explicit_paths_write_scope_and_atomic_write(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"
    token_path = tmp_path / "auth" / "write_token.json"
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

    assert observed["scopes"] == [CALENDAR_WRITE_SCOPE]
    assert observed["server"]["access_type"] == "offline"
    assert observed["server"]["prompt"] == "consent"
    assert observed["server"]["open_browser"] is False
    assert token_path.read_text(encoding="utf-8") == '{"token": "secret"}'


def test_oauth_rejects_missing_refresh_token(tmp_path: Path) -> None:
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text("{}", encoding="utf-8")

    class _Credentials:
        refresh_token = None

    class _Flow:
        def run_local_server(self, **kwargs: Any) -> _Credentials:
            return _Credentials()

    with pytest.raises(RuntimeError, match="no refresh token"):
        authorize(
            credentials_path=credentials_path,
            token_path=tmp_path / "write_token.json",
            open_browser=False,
            flow_loader=lambda path, scopes: _Flow(),
        )


def test_oauth_rejects_missing_credentials_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="OAuth client file not found"):
        authorize(
            credentials_path=tmp_path / "missing.json",
            token_path=tmp_path / "write_token.json",
            open_browser=False,
        )
