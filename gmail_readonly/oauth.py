"""Standalone installed-app OAuth bootstrap for the read-only Gmail scope."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from google_oauth_common.bootstrap import FlowLoader, authorize_installed_app, run_bootstrap_cli
from google_oauth_common.token_store import write_token_atomically

from gmail_readonly.core import GMAIL_READONLY_SCOPE

logger = logging.getLogger(__name__)


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
    authorize_installed_app(
        credentials_path=credentials_path,
        token_path=token_path,
        scope=GMAIL_READONLY_SCOPE,
        not_found_label="Gmail",
        token_writer=write_token_atomically,
        host=host,
        port=port,
        open_browser=open_browser,
        flow_loader=flow_loader,
    )


def main(argv: list[str] | None = None) -> int:
    """Run the standalone explicit-path OAuth command."""
    return run_bootstrap_cli(
        argv,
        description=__doc__,
        opening_message="Opening Google consent for read-only Gmail access.",
        success_label="Gmail read-only",
        logger=logger,
        authorize_fn=authorize,
    )


if __name__ == "__main__":
    sys.exit(main())
