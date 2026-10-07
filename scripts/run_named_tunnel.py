"""Start the webapp + cloudflared on a named (persistent) tunnel.

Used by ``webapp_tunnel_named.bat`` for headless / no-tray use. The tray already
does this same work as part of normal startup — only reach for this script when
running without the tray.

Starts the webapp through ``WebappManager`` — the tray's own adopt-or-spawn path
(cross-process start lock, Tailscale cert check, the same host/port/TLS flags) —
then ``cloudflared tunnel --config webapp/cloudflared.yml run``. The persistent
URL is written to ``webapp/last_tunnel_url.txt`` (with ``?token=…`` when an
``auth_token`` is configured).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

logger = logging.getLogger("run_named_tunnel")

# scripts/ is sys.path[0] when run as a file — put the repo root on it too, so `src` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tray import cloudflared_proc  # noqa: E402 — needs the repo root on sys.path
from app.webapp.manager import WebappManager, WebappManagerConfig  # noqa: E402
from src.paths import PROJECT_ROOT  # noqa: E402
from src.subprocess_flags import NO_WINDOW_NEW_GROUP  # noqa: E402
from src.webapp_config import load_webapp_config  # noqa: E402

DEFAULT_CONFIG = PROJECT_ROOT / "webapp" / "cloudflared.yml"
SAMPLE_CONFIG = PROJECT_ROOT / "config" / "cloudflared.sample.yml"


def _spawn_cloudflared(config_path: Path) -> subprocess.Popen[str]:
    bin_path = shutil.which("cloudflared")
    if bin_path is None:
        raise SystemExit(
            "❌ cloudflared not found on PATH. Install: "
            "winget install Cloudflare.cloudflared"
        )
    cmd = [bin_path, "tunnel", "--config", str(config_path), "run"]
    logger.info(f"🌐 Starting cloudflared: {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=NO_WINDOW_NEW_GROUP,
    )


def _read_auth_token() -> str:
    try:
        from src.webapp_config import load_webapp_config

        return (load_webapp_config().auth_token or "").strip()
    except Exception as exc:
        logger.debug(f"could not read auth_token: {exc}")
        return ""


def _stream(proc: subprocess.Popen[str]) -> None:
    for line in proc.stdout or ():
        sys.stdout.write(line)
        sys.stdout.flush()


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    config_path = Path(os.environ.get("CLOUDFLARED_CONFIG", str(DEFAULT_CONFIG)))
    if not config_path.exists():
        logger.error(
            f"❌ {config_path} missing. Copy {SAMPLE_CONFIG} to "
            f"{config_path} and fill in your tunnel UUID + hostname."
        )
        return 1

    hostname = cloudflared_proc.read_hostname(config_path)
    if hostname:
        logger.info(f"🌍 Public hostname: https://{hostname}")

    wcfg = load_webapp_config()
    manager = WebappManager(
        WebappManagerConfig(
            enabled=wcfg.enabled,
            host=wcfg.host,
            port=int(os.environ.get("WR_WEBAPP_PORT", wcfg.port)),
        )
    )
    try:
        status = manager.start(wait=True)
    except RuntimeError as exc:
        logger.error(f"❌ webapp start failed: {exc}")
        return 1
    if not status.running:
        logger.error(f"❌ webapp is not running ({status.detail})")
        return 1

    cloudflared = _spawn_cloudflared(config_path)
    threading.Thread(target=_stream, args=(cloudflared,), daemon=True).start()

    if hostname:
        cloudflared_proc.persist_tunnel_url(hostname, _read_auth_token())

    try:
        cloudflared.wait()
    except KeyboardInterrupt:
        logger.info("⏹️  Ctrl+C — shutting down")
    finally:
        cloudflared_proc.stop_proc(cloudflared, "cloudflared")
        manager.stop()  # a no-op when the webapp was adopted rather than spawned here
        cloudflared_proc.cleanup_url_file()

    return 0


if __name__ == "__main__":
    sys.exit(main())
