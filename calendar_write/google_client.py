"""Official Google API adapter for the portable write-scope Calendar core."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from calendar_write.core import CALENDAR_WRITE_SCOPE

CredentialLoader = Callable[[str, list[str]], Any]
RequestFactory = Callable[[], Any]
ServiceBuilder = Callable[..., Any]


class GoogleCalendarWriteClient:
    """Narrow adapter over the official Calendar discovery client (insert/delete only)."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def insert_event(self, *, calendar_id: str, event: dict[str, Any]) -> dict[str, Any]:
        """Create ``event`` on ``calendar_id`` and return the created resource."""
        result: dict[str, Any] = (
            self._service.events().insert(calendarId=calendar_id, body=event).execute()
        )
        return result

    def delete_event(self, *, calendar_id: str, event_id: str) -> None:
        """Delete a previously created event by id."""
        self._service.events().delete(calendarId=calendar_id, eventId=event_id).execute()

    def close(self) -> None:
        http = getattr(self._service, "_http", None)
        close = getattr(http, "close", None)
        if callable(close):
            close()


def build_google_calendar_write_client(
    token_path: Path,
    *,
    credential_loader: CredentialLoader | None = None,
    request_factory: RequestFactory | None = None,
    service_builder: ServiceBuilder | None = None,
) -> GoogleCalendarWriteClient:
    """Load/refresh the write-scope OAuth token and build the official client."""
    if not token_path.is_file():
        raise FileNotFoundError(
            "Calendar write OAuth token missing; run scripts/auth_calendar_write.py interactively"
        )

    if credential_loader is None or request_factory is None or service_builder is None:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credential_loader = credential_loader or Credentials.from_authorized_user_file
        request_factory = request_factory or Request
        service_builder = service_builder or build

    credentials = credential_loader(str(token_path), [CALENDAR_WRITE_SCOPE])
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(request_factory())
        write_token_atomically(token_path, credentials.to_json())
    if not credentials.valid:
        raise RuntimeError("Calendar write OAuth token is invalid or has been revoked")
    service = service_builder(
        "calendar",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )
    return GoogleCalendarWriteClient(service)


def write_token_atomically(path: Path, token_json: str) -> None:
    """Persist an OAuth token atomically without logging its contents.

    Duplicated (not imported) from ``calendar_readonly.google_client``: the two
    packages are independently portable credential/scope boundaries, so this
    trivial helper stays self-contained rather than coupling them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(token_json, encoding="utf-8")
    temporary_path.replace(path)
