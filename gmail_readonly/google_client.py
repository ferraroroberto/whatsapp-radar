"""Official Google API adapter for the portable read-only Gmail core."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from gmail_readonly.core import GMAIL_READONLY_SCOPE, GmailReadClient

CredentialLoader = Callable[[str, list[str]], Any]
RequestFactory = Callable[[], Any]
ServiceBuilder = Callable[..., Any]


class GoogleGmailReadClient:
    """Narrow adapter over the official Gmail discovery client."""

    def __init__(self, service: Any) -> None:
        self._service = service

    def get_profile(self) -> dict[str, Any]:
        response: dict[str, Any] = self._service.users().getProfile(userId="me").execute()
        return response

    def list_labels(self) -> list[dict[str, Any]]:
        response = self._service.users().labels().list(userId="me").execute()
        return list(response.get("labels") or [])

    def list_message_ids(
        self,
        *,
        query: str,
        label_ids: list[str] | None = None,
    ) -> list[str]:
        message_ids: list[str] = []
        page_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {
                "userId": "me",
                "q": query,
                "maxResults": 500,
                "includeSpamTrash": False,
            }
            if label_ids:
                kwargs["labelIds"] = label_ids
            if page_token:
                kwargs["pageToken"] = page_token
            response = self._service.users().messages().list(**kwargs).execute()
            message_ids.extend(
                str(message["id"])
                for message in response.get("messages") or []
                if message.get("id")
            )
            page_token = response.get("nextPageToken")
            if not page_token:
                return message_ids

    def get_message(
        self,
        message_id: str,
        *,
        metadata_only: bool = False,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "userId": "me",
            "id": message_id,
            "format": "metadata" if metadata_only else "full",
        }
        if metadata_only:
            kwargs["metadataHeaders"] = [
                "Message-ID",
                "In-Reply-To",
                "References",
                "From",
                "To",
                "Subject",
                "Date",
            ]
        response: dict[str, Any] = (
            self._service.users().messages().get(**kwargs).execute()
        )
        return response

    def close(self) -> None:
        http = getattr(self._service, "_http", None)
        close = getattr(http, "close", None)
        if callable(close):
            close()


def build_google_read_client(
    token_path: Path,
    *,
    credential_loader: CredentialLoader | None = None,
    request_factory: RequestFactory | None = None,
    service_builder: ServiceBuilder | None = None,
) -> GmailReadClient:
    """Load/refresh an OAuth token and build the official read-only client."""
    if not token_path.is_file():
        raise FileNotFoundError("Gmail OAuth token missing; run the OAuth bootstrap interactively")

    if credential_loader is None or request_factory is None or service_builder is None:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        credential_loader = credential_loader or Credentials.from_authorized_user_file
        request_factory = request_factory or Request
        service_builder = service_builder or build

    credentials = credential_loader(str(token_path), [GMAIL_READONLY_SCOPE])
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(request_factory())
        write_token_atomically(token_path, credentials.to_json())
    if not credentials.valid:
        raise RuntimeError("Gmail OAuth token is invalid or has been revoked")
    service = service_builder(
        "gmail",
        "v1",
        credentials=credentials,
        cache_discovery=False,
    )
    return GoogleGmailReadClient(service)


def write_token_atomically(path: Path, token_json: str) -> None:
    """Persist an OAuth token atomically without logging its contents."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(token_json, encoding="utf-8")
    temporary_path.replace(path)
