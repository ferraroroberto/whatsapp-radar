"""Standalone installed-app OAuth bootstrap for the read-only Gmail scope."""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from gmail_readonly.core import GMAIL_READONLY_SCOPE
from gmail_readonly.google_client import write_token_atomically

logger = logging.getLogger(__name__)
FlowLoader = Callable[[str, list[str]], Any]


def authorize(
    *,
    credentials_path: Path,
    token_path: Path,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    flow_loader: FlowLoader | None = None,
) -> None:
    """Run consent using explicit paths and persist the resulting refresh token."""
    if not credentials_path.is_file():
        raise FileNotFoundError(f"Gmail OAuth client file not found: {credentials_path}")
    if flow_loader is None:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow_loader = InstalledAppFlow.from_client_secrets_file
    flow = flow_loader(str(credentials_path), [GMAIL_READONLY_SCOPE])
    credentials = flow.run_local_server(
        host=host,
        port=port,
        access_type="offline",
        prompt="consent",
        open_browser=open_browser,
    )
    if not credentials.refresh_token:
        raise RuntimeError("Google returned no refresh token; revoke the old grant and retry")
    write_token_atomically(token_path, credentials.to_json())


def main(argv: list[str] | None = None) -> int:
    """Run the standalone explicit-path OAuth command."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--token", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=0, type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        logger.info("ℹ️ Opening Google consent for read-only Gmail access.")
        authorize(
            credentials_path=args.credentials,
            token_path=args.token,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        logger.error("❌ %s", exc)
        return 1
    logger.info("✅ Gmail read-only token stored at %s", args.token)
    logger.info("ℹ️ Never copy the token into config, documentation, or logs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
