"""The Calendar OAuth bootstrap scripts mint tokens where the runtime reads them (#351)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("module", "token_env"),
    [
        ("scripts.auth_calendar", "WR_CALENDAR_TOKEN_PATH"),
        ("scripts.auth_calendar_write", "WR_CALENDAR_WRITE_TOKEN_PATH"),
    ],
)
def test_bootstrap_uses_the_configured_paths(
    module: str,
    token_env: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    creds = tmp_path / "creds.json"
    token = tmp_path / "token.json"
    monkeypatch.setenv("WR_CALENDAR_CREDENTIALS_PATH", str(creds))
    monkeypatch.setenv(token_env, str(token))

    script = importlib.import_module(module)
    seen: list[list[str]] = []

    def fake_oauth_main(argv: list[str]) -> int:
        seen.append(argv)
        return 0

    monkeypatch.setattr(script, "oauth_main", fake_oauth_main)

    assert script.main() == 0
    assert seen == [["--credentials", str(creds), "--token", str(token)]]
