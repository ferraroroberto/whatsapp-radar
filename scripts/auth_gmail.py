"""Interactive one-time OAuth bootstrap for the headless Gmail connector."""

from __future__ import annotations

import logging
import sys

from src.config import load_config
from src.connector.gmail import GMAIL_READONLY_SCOPE, write_gmail_token

logger = logging.getLogger(__name__)


def main() -> int:
    """Run Google's installed-app consent flow and persist the refresh token."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    config = load_config().gmail
    if not config.credentials_path.is_file():
        logger.error("❌ Gmail OAuth client file not found: %s", config.credentials_path)
        logger.error("Follow docs/gmail-bootstrap.md before running this module.")
        return 1

    from google_auth_oauthlib.flow import InstalledAppFlow

    logger.info("ℹ️ Opening Google consent for read-only Gmail access.")
    flow = InstalledAppFlow.from_client_secrets_file(
        str(config.credentials_path),
        [GMAIL_READONLY_SCOPE],
    )
    credentials = flow.run_local_server(
        host="127.0.0.1",
        port=0,
        access_type="offline",
        prompt="consent",
        open_browser=True,
    )
    if not credentials.refresh_token:
        logger.error("❌ Google returned no refresh token; revoke the old grant and retry.")
        return 1
    write_gmail_token(config.token_path, credentials.to_json())
    logger.info("✅ Gmail read-only token stored at %s", config.token_path)
    logger.info("ℹ️ The token is gitignored; never copy it into config or documentation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
