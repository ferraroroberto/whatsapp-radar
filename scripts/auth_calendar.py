"""Interactive one-time OAuth bootstrap for the read-only Calendar source (#160).

Thin WhatsApp Radar wrapper around ``calendar_readonly.oauth`` using this repo's
standard ignored paths. Run once, interactively, from the repository root:

    .\\.venv\\Scripts\\python.exe -m scripts.auth_calendar

It opens a loopback browser consent for ``calendar.readonly`` only and writes
``auth/calendar/token.json``. The scheduled checks refresh access tokens from
that file automatically and never launch a browser.
"""

from __future__ import annotations

import sys

from calendar_readonly.oauth import main as oauth_main

_CREDENTIALS_PATH = "auth/calendar/credentials.json"
_TOKEN_PATH = "auth/calendar/token.json"


def main() -> int:
    """Run the portable OAuth command using WhatsApp Radar's standard paths."""
    return oauth_main(["--credentials", _CREDENTIALS_PATH, "--token", _TOKEN_PATH])


if __name__ == "__main__":
    sys.exit(main())
