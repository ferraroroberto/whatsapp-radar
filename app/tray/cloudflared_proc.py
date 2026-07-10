"""Shared cloudflared process lifecycle — used by both the tray and the
standalone named-tunnel script.

Both surfaces start ``cloudflared tunnel ... run``, advertise the resulting
public URL by writing ``webapp/last_tunnel_url.txt``, and tear the process down
on the same CTRL_BREAK -> terminate -> wait -> kill sequence. Keeping that here
means the two call sites can't silently drift on the stop/cleanup details.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from src.webapp_config import append_auth_token

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TUNNEL_URL_FILE = PROJECT_ROOT / "webapp" / "last_tunnel_url.txt"


def read_hostname(config_path: Path) -> str | None:
    """Pull the first ingress[].hostname out of the cloudflared config."""
    if not config_path.exists():
        return None
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(f"⚠️  Could not parse {config_path}: {exc}")
        return None
    for entry in data.get("ingress") or []:
        if isinstance(entry, dict) and entry.get("hostname"):
            return str(entry["hostname"]).strip()
    return None


def persist_tunnel_url(hostname: str, token: str) -> None:
    """Write the public URL (with ``?token=`` when set) to ``TUNNEL_URL_FILE``."""
    url = f"https://{hostname}"
    if token:
        url = append_auth_token(url, token)
    try:
        TUNNEL_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
        TUNNEL_URL_FILE.write_text(url + "\n", encoding="utf-8")
        logger.info(f"📡 Tunnel URL → {TUNNEL_URL_FILE}")
        logger.info(f"   {url}")
    except OSError as exc:
        logger.warning(f"⚠️  Could not write {TUNNEL_URL_FILE}: {exc}")


def stop_proc(proc: subprocess.Popen[Any], name: str) -> None:
    """Stop one child process: CTRL_BREAK (win) -> terminate -> wait(5) -> kill -> wait(3)."""
    try:
        logger.info(f"🛑 Stopping {name} (pid={proc.pid})")
        if sys.platform == "win32":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except Exception:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"{name} stop failed: {exc}")


def cleanup_url_file() -> None:
    """Remove the advertised tunnel-URL file, if present."""
    try:
        if TUNNEL_URL_FILE.exists():
            TUNNEL_URL_FILE.unlink()
    except OSError:
        pass
