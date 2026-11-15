"""The origin that run links in Telegram messages point at.

Precedence: ``WR_PUBLIC_BASE_URL`` env > ``public_base_url`` in the gitignored
``config/webapp_config.json`` > that file's ``webauthn_origin``, the https
origin the phone already connects to. Empty means messages carry no link.
"""

from __future__ import annotations

import os


def parse() -> str:
    # Imported lazily to avoid a config import cycle.
    from src.webapp_config import load_webapp_config

    wcfg = load_webapp_config()
    base = os.environ.get("WR_PUBLIC_BASE_URL") or wcfg.public_base_url or wcfg.webauthn_origin
    return base.strip().rstrip("/")
