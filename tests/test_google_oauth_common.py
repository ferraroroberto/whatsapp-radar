"""Portability contract for the shared OAuth-bootstrap package.

``google_oauth_common`` is copied byte-for-byte alongside ``gmail_readonly``,
``calendar_readonly``, or ``calendar_write`` when one of them is lifted into
another repo (docs/gmail-reuse.md). It must stay free of imports from this
repo's application code and must not reach back into any of the three
concrete clients it serves — the dependency direction is one-way.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


def test_component_imports_no_application_or_client_modules() -> None:
    component_root = Path(__file__).parents[1] / "google_oauth_common"
    imported_modules: set[str] = set()
    for path in component_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)
    forbidden_prefixes = (
        "src",
        "app",
        "scripts",
        "gmail_readonly",
        "calendar_readonly",
        "calendar_write",
    )
    assert not any(
        module == prefix or module.startswith(f"{prefix}.")
        for module in imported_modules
        for prefix in forbidden_prefixes
    )


def test_every_builder_bounds_its_transport_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No builder may leave a service on httplib2's unbounded default (#180, #298).

    ``build(..., credentials=...)`` silently keeps httplib2's default transport,
    whose default timeout is ``None`` — a stalled connection then hangs a
    scheduled job forever instead of failing after a bounded wait. Gmail fixed
    that first; this asserts all three builders carry the same bound.
    """
    import googleapiclient.discovery
    from calendar_readonly.google_client import build_google_calendar_client
    from calendar_write.google_client import build_google_calendar_write_client
    from gmail_readonly.google_client import build_google_read_client
    from google_oauth_common.transport import DEFAULT_REQUEST_TIMEOUT_S

    class _Credentials:
        expired = False
        valid = True
        refresh_token = "present"

        def to_json(self) -> str:
            return "{}"

    observed: list[dict[str, object]] = []

    def fake_build(serviceName: str, version: str, **kwargs: object) -> object:
        observed.append(kwargs)
        return object()

    monkeypatch.setattr(googleapiclient.discovery, "build", fake_build)

    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    loader = lambda _path, _scopes: _Credentials()  # noqa: E731 - one-line test seam

    for builder in (
        build_google_read_client,
        build_google_calendar_client,
        build_google_calendar_write_client,
    ):
        builder(
            token_path,
            credential_loader=loader,
            request_factory=lambda: "request",
        )

    assert len(observed) == 3
    for kwargs in observed:
        assert "credentials" not in kwargs, "credentials= leaves the default transport"
        assert kwargs["http"].http.timeout == DEFAULT_REQUEST_TIMEOUT_S


def test_build_service_injected_builder_gets_credentials_and_owns_transport() -> None:
    """The legacy test seam: an injected builder receives ``credentials=``, no ``http=``."""
    from google_oauth_common.transport import build_service

    calls: list[tuple[str, str, dict[str, object]]] = []

    def builder(api: str, version: str, **kwargs: object) -> str:
        calls.append((api, version, kwargs))
        return "service"

    creds = object()
    assert build_service("gmail", "v1", creds, service_builder=builder) == "service"
    assert calls == [("gmail", "v1", {"credentials": creds, "cache_discovery": False})]


class _RefreshingCredentials:
    """Expired credentials whose ``refresh`` raises the queued errors, then succeeds."""

    refresh_token = "present"

    def __init__(self, failures: list[Exception]) -> None:
        self.expired = True
        self.valid = False
        self.refresh_calls = 0
        self._failures = list(failures)

    def refresh(self, _request: object) -> None:
        self.refresh_calls += 1
        if self._failures:
            raise self._failures.pop(0)
        self.expired = False
        self.valid = True

    def to_json(self) -> str:
        return "{}"


def _refresh(
    tmp_path: Path, credentials: _RefreshingCredentials, sleeps: list[float]
) -> list[Path]:
    from google_oauth_common.credentials import load_or_refresh_credentials

    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    written: list[Path] = []
    load_or_refresh_credentials(
        token_path,
        "scope",
        missing_token_message="missing",
        invalid_token_message="invalid",
        token_writer=lambda path, _text: written.append(path),
        credential_loader=lambda _path, _scopes: credentials,
        request_factory=lambda: "request",
        sleep=sleeps.append,
    )
    return written


def test_refresh_retries_a_transient_transport_error(tmp_path: Path) -> None:
    """A dropped TLS connection on the token endpoint must not fail the run (#384)."""
    from google.auth.exceptions import TransportError

    credentials = _RefreshingCredentials([TransportError("SSLEOFError"), TransportError("reset")])
    sleeps: list[float] = []

    written = _refresh(tmp_path, credentials, sleeps)

    assert credentials.refresh_calls == 3
    assert len(sleeps) == 2 and all(delay > 0 for delay in sleeps)
    assert written == [tmp_path / "token.json"], "the refreshed token is persisted once"


def test_refresh_gives_up_with_a_distinct_transient_error(tmp_path: Path) -> None:
    from google.auth.exceptions import TransportError
    from google_oauth_common.credentials import REFRESH_ATTEMPTS, TransientNetworkError

    credentials = _RefreshingCredentials([TransportError("SSLEOFError")] * 10)
    sleeps: list[float] = []

    with pytest.raises(TransientNetworkError, match="SSLEOFError") as excinfo:
        _refresh(tmp_path, credentials, sleeps)

    assert credentials.refresh_calls == REFRESH_ATTEMPTS
    assert len(sleeps) == REFRESH_ATTEMPTS - 1, "no sleep after the final attempt"
    assert isinstance(excinfo.value, RuntimeError), "the CLI's failed-run handler catches it"
    assert "network" in str(excinfo.value).lower()


def test_refresh_does_not_retry_an_auth_error(tmp_path: Path) -> None:
    """A revoked token (``invalid_grant``) is a real failure: one attempt, loud."""
    from google.auth.exceptions import RefreshError
    from google_oauth_common.credentials import TransientNetworkError

    credentials = _RefreshingCredentials([RefreshError("invalid_grant")] * 5)
    sleeps: list[float] = []

    with pytest.raises(RefreshError) as excinfo:
        _refresh(tmp_path, credentials, sleeps)

    assert not isinstance(excinfo.value, TransientNetworkError)
    assert credentials.refresh_calls == 1
    assert sleeps == []


def test_calendar_list_events_asks_the_api_client_to_retry_transient_errors() -> None:
    """The read call retries SSL/connection drops itself, like the token refresh."""
    from datetime import UTC, datetime

    from calendar_readonly.google_client import GoogleCalendarReadClient

    seen: list[dict[str, object]] = []

    class _Request:
        def execute(self, **kwargs: object) -> dict[str, object]:
            seen.append(kwargs)
            return {"items": []}

    class _Events:
        def list(self, **_kwargs: object) -> _Request:
            return _Request()

    class _Service:
        def events(self) -> _Events:
            return _Events()

    moment = datetime(2026, 10, 12, tzinfo=UTC)
    GoogleCalendarReadClient(_Service()).list_events(
        calendar_id="primary", time_min=moment, time_max=moment
    )

    assert seen and all(kwargs.get("num_retries", 0) >= 1 for kwargs in seen)
