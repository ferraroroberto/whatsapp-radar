"""Interactive one-time OAuth bootstrap for the headless Gmail connector."""

from __future__ import annotations

import sys

from gmail_readonly.oauth import main as oauth_main

from src.config import load_config


def main() -> int:
    """Run the portable OAuth command using WhatsApp Radar's configured paths."""
    config = load_config().gmail
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
