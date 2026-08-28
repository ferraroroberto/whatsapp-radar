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
