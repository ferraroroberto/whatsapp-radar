"""Official Google API adapter for the portable read-only Gmail core."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from google_oauth_common.credentials import load_or_refresh_credentials
from google_oauth_common.token_store import write_token_atomically as write_token_atomically

from gmail_readonly.core import GMAIL_READONLY_SCOPE, GmailReadClient

CredentialLoader = Callable[[str, list[str]], Any]
RequestFactory = Callable[[], Any]
ServiceBuilder = Callable[..., Any]

# Gmail's batch endpoint accepts up to 100 calls; Google recommends staying ≤50.
_BATCH_SIZE = 50

# Gmail's per-user rate budget is ~250 quota units/second and a message GET costs
# 5 units, so one 50-GET batch consumes a full second of budget. Firing batches
# back-to-back trips the limiter mid-window (observed live while building #180),
# hence one pause between batches and bounded backoff for items that still 429.
_BATCH_PAUSE_S = 1.0
_RETRY_ATTEMPTS = 3
_RETRYABLE_STATUSES = frozenset({429, 500, 503})

# httplib2's default is no timeout at all — a dropped connection would block a
# sync forever (#180). Every request made through this adapter gets this bound.
DEFAULT_REQUEST_TIMEOUT_S = 60


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
        response: dict[str, Any] = self._get_request(message_id, metadata_only).execute()
        return response

    def get_messages(
        self,
        message_ids: list[str],
        *,
        metadata_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Fetch many messages via the API batch endpoint (≤50 per round-trip).

        Sequential per-message GETs are latency-dominated — a 30-day window costs
        hundreds of round-trips (#180); batching collapses that to a handful.
        Batches are paced against the per-user rate budget and rate-limited items
        retry with exponential backoff; results come back in ``message_ids``
        order, and the first non-retryable (or retry-exhausted) failure aborts
        the whole read so callers never advance on a partial window.
        """
        collected: dict[str, dict[str, Any]] = {}
        for start in range(0, len(message_ids), _BATCH_SIZE):
            if start:
                time.sleep(_BATCH_PAUSE_S)
            self._execute_batch(
                message_ids[start : start + _BATCH_SIZE], metadata_only, collected
            )
        return [collected[mid] for mid in message_ids if mid in collected]

    def _execute_batch(
        self,
        chunk: list[str],
        metadata_only: bool,
        collected: dict[str, dict[str, Any]],
    ) -> None:
        remaining = list(chunk)
        for attempt in range(_RETRY_ATTEMPTS + 1):
            failures = self._attempt_batch(remaining, metadata_only, collected)
            if not failures:
                return
            fatal = [exc for _, exc in failures if not _is_retryable(exc)]
            if fatal or attempt == _RETRY_ATTEMPTS:
                raise (fatal or [failures[0][1]])[0]
            remaining = [message_id for message_id, _ in failures]
            time.sleep(2**attempt)

    def _attempt_batch(
        self,
        message_ids: list[str],
        metadata_only: bool,
        collected: dict[str, dict[str, Any]],
    ) -> list[tuple[str, Exception]]:
        failures: list[tuple[str, Exception]] = []

        def _collect(request_id: str, response: Any, exception: Exception | None) -> None:
            if exception is not None:
                failures.append((request_id, exception))
            elif response is not None:
                collected[request_id] = response

        batch = self._service.new_batch_http_request(callback=_collect)
        for message_id in message_ids:
            batch.add(
                self._get_request(message_id, metadata_only),
                request_id=message_id,
            )
        batch.execute()
        return failures

    def _get_request(self, message_id: str, metadata_only: bool) -> Any:
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
        return self._service.users().messages().get(**kwargs)

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
    request_timeout_s: int = DEFAULT_REQUEST_TIMEOUT_S,
) -> GmailReadClient:
    """Load/refresh an OAuth token and build the official read-only client."""
    credentials = load_or_refresh_credentials(
        token_path,
        GMAIL_READONLY_SCOPE,
        missing_token_message="Gmail OAuth token missing; run the OAuth bootstrap interactively",
        invalid_token_message="Gmail OAuth token is invalid or has been revoked",
        token_writer=write_token_atomically,
        credential_loader=credential_loader,
        request_factory=request_factory,
    )

    injected_builder = service_builder is not None
    if service_builder is None:
        from googleapiclient.discovery import build

        service_builder = build

    if injected_builder:
        # Test seam: injected builders receive the legacy credentials kwarg and
        # own their transport entirely.
        service = service_builder(
            "gmail",
            "v1",
            credentials=credentials,
            cache_discovery=False,
        )
    else:
        import httplib2  # type: ignore[import-untyped]
        from google_auth_httplib2 import AuthorizedHttp  # type: ignore[import-untyped]

        service = service_builder(
            "gmail",
            "v1",
            http=AuthorizedHttp(credentials, http=httplib2.Http(timeout=request_timeout_s)),
            cache_discovery=False,
        )
    return GoogleGmailReadClient(service)


def _is_retryable(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    return status in _RETRYABLE_STATUSES or isinstance(exc, TimeoutError)
