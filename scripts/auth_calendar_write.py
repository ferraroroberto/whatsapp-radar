"""Interactive one-time OAuth bootstrap for the write-scope Calendar events grant (#217).

Thin WhatsApp Radar wrapper around ``calendar_write.oauth`` using the paths from
``load_config().calendar`` (so a ``WR_CALENDAR_*`` env or ``local.json`` override
is honoured — the token lands where the runtime reads it). Run once,
interactively, from the repository root:

    .\.venv\Scripts\python.exe -m scripts.auth_calendar_write

It opens a loopback browser consent for ``calendar.events`` only — separate
from the read-only ``calendar.readonly`` grant minted by
``scripts.auth_calendar`` — and writes the configured write token (default
``auth/calendar/write_token.json``). Event creation (Step 4/5 of #206) refreshes
access tokens from that file automatically and never launches a browser.
"""

from __future__ import annotations

import sys

from calendar_write.oauth import main as oauth_main

from src.config import load_config


def main() -> int:
    """Run the portable OAuth command using WhatsApp Radar's configured paths."""
    config = load_config().calendar
    return oauth_main(
        [
            "--credentials",
            str(config.credentials_path),
            "--token",
            str(config.write_token_path),
        ]
    )


if __name__ == "__main__":
    sys.exit(main())
