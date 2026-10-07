"""Interactive one-time OAuth bootstrap for the read-only Calendar source (#160).

Thin WhatsApp Radar wrapper around ``calendar_readonly.oauth`` using the paths
from ``load_config().calendar`` (so a ``WR_CALENDAR_*`` env or ``local.json``
override is honoured — the token lands where the runtime reads it). Run once,
interactively, from the repository root:

    .\.venv\Scripts\python.exe -m scripts.auth_calendar

It opens a loopback browser consent for ``calendar.readonly`` only and writes
the configured token (default ``auth/calendar/token.json``). The scheduled
checks refresh access tokens from that file automatically and never launch a
browser.
"""

from __future__ import annotations

import sys

from calendar_readonly.oauth import main as oauth_main

from src.config import load_config


def main() -> int:
    """Run the portable OAuth command using WhatsApp Radar's configured paths."""
    config = load_config().calendar
    return oauth_main(
        [
            "--credentials",
            str(config.credentials_path),
            "--token",
            str(config.token_path),
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
