"""Shared OAuth token load/refresh/validate step for the portable Google packages."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# The token endpoint is one HTTPS call a scheduled job makes every few minutes, so
# a dropped connection (TLS EOF, reset, timeout) is a matter of when, not if.
# Three attempts with a 2 s then 4 s pause ride out a blip without stalling a job.
REFRESH_ATTEMPTS = 3
REFRESH_BACKOFF_S = 2.0

CredentialLoader = Callable[[str, list[str]], Any]
RequestFactory = Callable[[], Any]
TokenWriter = Callable[[Path, str], None]


class TransientNetworkError(RuntimeError):
    """The token endpoint stayed unreachable for every refresh attempt.

    Deliberately not an auth error: the stored token may be perfectly good. A
    ``RuntimeError`` so callers' existing failed-run handling reports it, with a
    message that names the network as the cause instead of the credentials.
    """


def load_or_refresh_credentials(
    token_path: Path,
    scope: str,
    *,
    missing_token_message: str,
    invalid_token_message: str,
    token_writer: TokenWriter,
    credential_loader: CredentialLoader | None = None,
    request_factory: RequestFactory | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Load a persisted OAuth token, refresh it if expired, and validate it.

    Returns the (possibly refreshed) ``google.oauth2.credentials.Credentials``
    instance. Building the API service from it stays with each caller, since the
    discovery collection/version differs per package; the transport does not —
    every caller wraps these credentials in
    :func:`google_oauth_common.transport.bounded_authorized_http`.

    A refresh that fails with ``google.auth.exceptions.TransportError`` (a
    connection-level failure) is retried a bounded number of times, then raised
    as :class:`TransientNetworkError`. ``RefreshError`` (``invalid_grant``, a
    revoked token) is a sibling class, never retried, and propagates unchanged.
    """
    if not token_path.is_file():
        raise FileNotFoundError(missing_token_message)

    if credential_loader is None or request_factory is None:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials

        credential_loader = credential_loader or Credentials.from_authorized_user_file
        request_factory = request_factory or Request

    credentials = credential_loader(str(token_path), [scope])
    if credentials.expired and credentials.refresh_token:
        _refresh_with_retry(credentials, request_factory, sleep)
        token_writer(token_path, credentials.to_json())
    if not credentials.valid:
        raise RuntimeError(invalid_token_message)
    return credentials


def _refresh_with_retry(
    credentials: Any, request_factory: RequestFactory, sleep: Callable[[float], None]
) -> None:
    from google.auth.exceptions import TransportError

    for attempt in range(1, REFRESH_ATTEMPTS + 1):
        try:
            credentials.refresh(request_factory())
            return
        except TransportError as exc:
            if attempt == REFRESH_ATTEMPTS:
                raise TransientNetworkError(
                    f"Google token refresh failed after {attempt} attempts "
                    f"(network error, not an auth failure): {exc}"
                ) from exc
            delay = REFRESH_BACKOFF_S * attempt
            logger.warning(
                "⚠️ Google token refresh hit a transient network error "
                "(attempt %d/%d), retrying in %.0fs: %s",
                attempt, REFRESH_ATTEMPTS, delay, exc,
            )
            sleep(delay)
