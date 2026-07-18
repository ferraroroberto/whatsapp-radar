"""Official Google API adapter for the portable read-only Calendar core."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from calendar_readonly.core import CALENDAR_READONLY_SCOPE

CredentialLoader = Callable[[str, list[str]], Any]
RequestFactory = Callable[[], Any]
ServiceBuilder = Callable[..., Any]


class GoogleCalendarReadClient:
    """Narrow adapter over the official Calendar discovery client (read-only)."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def list_events(
        self,
        *,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
    ) -> list[dict[str, Any]]:
        """Return expanded single events in ``[time_min, time_max)``, time-ordered."""
        events: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "calendarId": calendar_id,
                "timeMin": _rfc3339(time_min),
                "timeMax": _rfc3339(time_max),
                "singleEvents": True,
                "orderBy": "startTime",
                "maxResults": 250,
            }
            if page_token:
                kwargs["pageToken"] = page_token
            response = self._service.events().list(**kwargs).execute()
            events.extend(response.get("items") or [])
            page_token = response.get("nextPageToken")
            if not page_token:
                return events

    def calendar_summary(self, calendar_id: str) -> str:
        """Return the calendar's display summary (a cheap reachability probe)."""
        response: dict[str, Any] = (
            self._service.calendars().get(calendarId=calendar_id).execute()
        )
        return str(response.get("summary") or calendar_id)

    def close(self) -> None:
        http = getattr(self._service, "_http", None)
        close = getattr(http, "close", None)
        if callable(close):
            close()


def build_google_calendar_client(
    token_path: Path,
    *,
    credential_loader: CredentialLoader | None = None,
    request_factory: RequestFactory | None = None,
    service_builder: ServiceBuilder | None = None,
) -> GoogleCalendarReadClient:
    """Load/refresh an OAuth token and build the official read-only client."""
    if not token_path.is_file():
        raise FileNotFoundError(
            "Calendar OAuth token missing; run the OAuth bootstrap interactively"
        )

    if credential_loader is None or request_factory is None or service_builder is None:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credential_loader = credential_loader or Credentials.from_authorized_user_file
        request_factory = request_factory or Request
        service_builder = service_builder or build

    credentials = credential_loader(str(token_path), [CALENDAR_READONLY_SCOPE])
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(request_factory())
        write_token_atomically(token_path, credentials.to_json())
    if not credentials.valid:
        raise RuntimeError("Calendar OAuth token is invalid or has been revoked")
    service = service_builder(
        "calendar",
        "v3",
        credentials=credentials,
        cache_discovery=False,
    )
    return GoogleCalendarReadClient(service)


def write_token_atomically(path: Path, token_json: str) -> None:
    """Persist an OAuth token atomically without logging its contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(token_json, encoding="utf-8")
    temporary_path.replace(path)


def _rfc3339(moment: datetime) -> str:
    """Calendar's timeMin/timeMax require an RFC-3339 timestamp with an offset."""
    if moment.tzinfo is None:
        return moment.astimezone().isoformat()
    return moment.isoformat()
