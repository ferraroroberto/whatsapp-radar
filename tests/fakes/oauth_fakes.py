"""Offline OAuth fakes shared by the portable Google client contract tests.

Each loader records what it was called with into the caller's ``observed`` dict, so a test
asserts on the scopes and paths the client under test passed in.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class _ExpiredCredentials:
    expired = True
    refresh_token = "present"
    valid = True

    def __init__(self, observed: dict[str, Any]) -> None:
        self._observed = observed

    def refresh(self, request: object) -> None:
        self._observed["request"] = request

    def to_json(self) -> str:
        return '{"refreshed": true}'


class _FreshCredentials:
    refresh_token = "present"

    def to_json(self) -> str:
        return '{"token": "secret"}'


class _Flow:
    def __init__(self, observed: dict[str, Any]) -> None:
        self._observed = observed

    def run_local_server(self, **kwargs: Any) -> _FreshCredentials:
        self._observed["server"] = kwargs
        return _FreshCredentials()


def expired_credential_loader(observed: dict[str, Any]) -> Callable[[str, list[str]], Any]:
    """A token loader whose credentials are expired and refresh on demand."""
    credentials = _ExpiredCredentials(observed)

    def load_credentials(path: str, scopes: list[str]) -> _ExpiredCredentials:
        observed["token_path"] = path
        observed["scopes"] = scopes
        return credentials

    return load_credentials


def flow_loader(observed: dict[str, Any]) -> Callable[[str, list[str]], Any]:
    """An installed-app flow loader whose local server yields fresh credentials."""

    def load_flow(path: str, scopes: list[str]) -> _Flow:
        observed["credentials_path"] = path
        observed["scopes"] = scopes
        return _Flow(observed)

    return load_flow
