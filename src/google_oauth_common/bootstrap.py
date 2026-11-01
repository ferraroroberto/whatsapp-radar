"""Shared installed-app OAuth consent flow and CLI skeleton.

Parametrized so each portable package's own ``oauth.py`` stays a thin wrapper
(scope, error-message labels, and log strings) around one real implementation.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

FlowLoader = Callable[[str, list[str]], Any]
TokenWriter = Callable[[Path, str], None]


def authorize_installed_app(
    *,
    credentials_path: Path,
    token_path: Path,
    scope: str,
    not_found_label: str,
    token_writer: TokenWriter,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    flow_loader: FlowLoader | None = None,
) -> None:
    """Run consent using explicit paths and persist the resulting refresh token."""
    if not credentials_path.is_file():
        raise FileNotFoundError(
            f"{not_found_label} OAuth client file not found: {credentials_path}"
        )
    if flow_loader is None:
        from google_auth_oauthlib.flow import InstalledAppFlow

        flow_loader = InstalledAppFlow.from_client_secrets_file
    flow = flow_loader(str(credentials_path), [scope])
    credentials = flow.run_local_server(
        host=host,
        port=port,
        access_type="offline",
        prompt="consent",
        open_browser=open_browser,
    )
    if not credentials.refresh_token:
        raise RuntimeError("Google returned no refresh token; revoke the old grant and retry")
    token_writer(token_path, credentials.to_json())


def run_bootstrap_cli(
    argv: list[str] | None,
    *,
    description: str | None,
    opening_message: str,
    success_label: str,
    logger: logging.Logger,
    authorize_fn: Callable[..., None],
) -> int:
    """Shared explicit-path CLI skeleton: parse args, run consent, report the result."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--credentials", required=True, type=Path)
    parser.add_argument("--token", required=True, type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=0, type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        logger.info("ℹ️ %s", opening_message)
        authorize_fn(
            credentials_path=args.credentials,
            token_path=args.token,
            host=args.host,
            port=args.port,
            open_browser=not args.no_browser,
        )
    except (FileNotFoundError, RuntimeError) as exc:
        logger.error("❌ %s", exc)
        return 1
    logger.info("✅ %s token stored at %s", success_label, args.token)
    logger.info("ℹ️ Never copy the token into config, documentation, or logs.")
    return 0
