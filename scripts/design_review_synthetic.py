"""Throwaway webapp for the design-review walk's synthetic run.

``/design-review whatsapp-radar --synthetic`` (and ``measure --e2e``) runs this
(``[design.review.synthetic]`` in ``.fleet.toml``) with the repo's venv python,
from the repo root. It boots the same disposable webapp the e2e autoboot does
(``tests.e2e.conftest.disposable_env``): a free loopback port, a temp directory
for every runtime path, the fixture connector and a sanitized seeded DB. It
prints ``URL=<base>`` once ``/healthz`` answers, then waits until stdin closes
and tears the webapp down.

Never points at the live tray on :8455 and never reads real chats.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.subprocess_flags import NO_WINDOW  # noqa: E402
from tests.e2e.conftest import disposable_env  # noqa: E402

logger = logging.getLogger("design_review_synthetic")
STARTUP_TIMEOUT_S = 60.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _healthy(base: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base}/healthz", timeout=2) as resp:  # noqa: S310
            return bool(resp.status == 200)
    except (urllib.error.URLError, OSError):
        return False


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    data_dir = Path(tempfile.mkdtemp(prefix="wr-synthetic-"))
    env = disposable_env(data_dir)
    log_fp = (data_dir / "webapp.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.webapp.server:app",
            "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
        ],
        cwd=str(ROOT), env=env, stdout=log_fp, stderr=subprocess.STDOUT,
        creationflags=NO_WINDOW,
    )
    try:
        deadline = time.time() + STARTUP_TIMEOUT_S
        while not _healthy(base):
            if proc.poll() is not None:
                logger.error("❌ synthetic webapp exited early; log: %s", data_dir / "webapp.log")
                return 1
            if time.time() > deadline:
                logger.error("❌ synthetic webapp not reachable after %ss", STARTUP_TIMEOUT_S)
                return 1
            time.sleep(0.1)
        logger.info("✅ synthetic webapp up on %s (data %s)", base, data_dir)
        print(f"URL={base}", flush=True)
        # The walk closes our stdin when it is done; reading to EOF blocks until then.
        sys.stdin.read()
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fp.close()
        shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
