"""Start uvicorn + cloudflared on a named (persistent) tunnel.

Used by ``webapp_tunnel_named.bat`` for headless / no-tray use. The tray already
does this same work as part of normal startup — only reach for this script when
running without the tray.

Boots uvicorn (HTTPS if ``webapp/certificates/cert.pem`` exists) then
``cloudflared tunnel --config webapp/cloudflared.yml run``. The persistent URL
is written to ``webapp/last_tunnel_url.txt`` (with ``?token=…`` when an
``auth_token`` is configured).
"""

from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger("run_named_tunnel")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.tray import cloudflared_proc  # noqa: E402 — needs PROJECT_ROOT on sys.path

DEFAULT_CONFIG = PROJECT_ROOT / "webapp" / "cloudflared.yml"
SAMPLE_CONFIG = PROJECT_ROOT / "config" / "cloudflared.sample.yml"
DEFAULT_PORT = 8455


def _have_listener(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _find_python() -> Path:
    venv_py = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
    if venv_py.exists():
        return venv_py
    venv_py = PROJECT_ROOT / ".venv" / "bin" / "python"
    if venv_py.exists():
        return venv_py
    return Path(sys.executable)


def _spawn_uvicorn(port: int) -> subprocess.Popen[bytes]:
    cert = PROJECT_ROOT / "webapp" / "certificates" / "cert.pem"
    key = PROJECT_ROOT / "webapp" / "certificates" / "key.pem"
    cmd = [
        str(_find_python()),
        "-m",
        "uvicorn",
        "app.webapp.server:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--log-level",
        "warning",
    ]
    if cert.exists() and key.exists():
        cmd.extend(["--ssl-keyfile", str(key), "--ssl-certfile", str(cert)])
    logger.info(f"🚀 Starting uvicorn: {' '.join(cmd)}")
    kw: dict[str, object] = dict(cwd=str(PROJECT_ROOT))
    if sys.platform == "win32":
        kw["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    return subprocess.Popen(cmd, **kw)  # type: ignore[arg-type]


def _wait_for_uvicorn(port: int, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _have_listener(port):
            return True
        time.sleep(0.3)
    return False


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

    port = int(os.environ.get("WR_WEBAPP_PORT", DEFAULT_PORT))
    uvicorn_proc: subprocess.Popen[bytes] | None = None
    if _have_listener(port):
        logger.info(f"🔗 Adopting existing webapp on :{port}")
    else:
        uvicorn_proc = _spawn_uvicorn(port)
        if not _wait_for_uvicorn(port):
            logger.error("❌ uvicorn failed to start within 15 s")
            if uvicorn_proc is not None:
                uvicorn_proc.terminate()
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
        for proc, name in ((cloudflared, "cloudflared"), (uvicorn_proc, "uvicorn")):
            if proc is None:
                continue
            cloudflared_proc.stop_proc(proc, name)
        cloudflared_proc.cleanup_url_file()

    return 0


if __name__ == "__main__":
    sys.exit(main())
