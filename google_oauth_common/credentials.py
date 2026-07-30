"""Shared OAuth token load/refresh/validate step for the portable Google packages."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

CredentialLoader = Callable[[str, list[str]], Any]
RequestFactory = Callable[[], Any]
TokenWriter = Callable[[Path, str], None]


def load_or_refresh_credentials(
    token_path: Path,
    scope: str,
    *,
    missing_token_message: str,
    invalid_token_message: str,
    token_writer: TokenWriter,
    credential_loader: CredentialLoader | None = None,
    request_factory: RequestFactory | None = None,
) -> Any:
    """Load a persisted OAuth token, refresh it if expired, and validate it.

    Returns the (possibly refreshed) ``google.oauth2.credentials.Credentials``
    instance. Building the API service from it stays with each caller, since
    that step differs per package (Gmail injects a bounded-timeout transport;
    Calendar passes credentials straight through).
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
        credentials.refresh(request_factory())
        token_writer(token_path, credentials.to_json())
    if not credentials.valid:
        raise RuntimeError(invalid_token_message)
    return credentials
