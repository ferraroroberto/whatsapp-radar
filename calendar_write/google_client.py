"""Official Google API adapter for the portable write-scope Calendar core."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from google_oauth_common.credentials import load_or_refresh_credentials
from google_oauth_common.token_store import write_token_atomically

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
    credentials = load_or_refresh_credentials(
        token_path,
        CALENDAR_WRITE_SCOPE,
        missing_token_message=(
            "Calendar write OAuth token missing; run scripts/auth_calendar_write.py interactively"
        ),
        invalid_token_message="Calendar write OAuth token is invalid or has been revoked",
        token_writer=write_token_atomically,
        credential_loader=credential_loader,
        request_factory=request_factory,
    )

    if service_builder is None:
        from googleapiclient.discovery import build

        service_builder = build

    service = service_builder(
        "calendar",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )
    return GoogleCalendarWriteClient(service)
