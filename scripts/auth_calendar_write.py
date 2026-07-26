"""Interactive one-time OAuth bootstrap for the write-scope Calendar events grant (#217).

Thin WhatsApp Radar wrapper around ``calendar_write.oauth`` using this repo's
standard ignored paths. Run once, interactively, from the repository root:

    .\\.venv\\Scripts\\python.exe -m scripts.auth_calendar_write

It opens a loopback browser consent for ``calendar.events`` only — separate
from the read-only ``calendar.readonly`` grant minted by
``scripts.auth_calendar`` — and writes ``auth/calendar/write_token.json``.
Event creation (Step 4/5 of #206) refreshes access tokens from that file
automatically and never launches a browser.
"""

from __future__ import annotations

import sys

from calendar_write.oauth import main as oauth_main

_CREDENTIALS_PATH = "auth/calendar/credentials.json"
_WRITE_TOKEN_PATH = "auth/calendar/write_token.json"


def main() -> int:
    """Run the portable OAuth command using WhatsApp Radar's standard paths."""
    return oauth_main(["--credentials", _CREDENTIALS_PATH, "--token", _WRITE_TOKEN_PATH])


if __name__ == "__main__":
    sys.exit(main())
