"""e2e harness: self-boot a disposable webapp on a free port.

Three run modes (issue #225, guard vendored from project-scaffolding's
`_e2e_live_guard.py` — issue #191/#194/#197):

* **Autoboot (default gate path).** ``WR_E2E_AUTOBOOT=1`` boots uvicorn on a
  free port against a throwaway DB and tears it down after.
  ``scripts/verify-before-ship.ps1`` / ``scripts/run-e2e.ps1`` set this — the
  suite always drives a disposable instance, never the live tray.
* **Live (explicit opt-in).** With no autoboot and a tray already serving on
  :8455, the vendored guard refuses via ``pytest.exit`` naming
  ``WR_E2E_LIVE`` unless it's set to ``1`` — a bare ``pytest tests/e2e`` must
  not load-test the instance you're actually using. Once opted in, this
  repo's own caller-side choice is to *adopt the tray read-only* and run only
  the ``@pytest.mark.live_safe`` subset (route-mocked tests with no
  unmocked mutating call) — never a reclaim/kill. `:8455` is a real
  daily-driver tray with real chat/family/passkey state (see this repo's
  CLAUDE.md "Safe restart"); a by-hand kill is unsafe and, if this repo ever
  needed to reclaim the port, only `tray.bat --restart` (PID-scoped to this
  repo's `.venv`) would be the canonical way. An unmarked test is excluded
  from live mode by default — allowlist, not denylist — so a newly added
  unmocked test can't silently start hitting the live backend.
* **Offline (nothing set, no tray up).** The suite skips, keeping plain
  ``pytest`` fully offline.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from tests.e2e._e2e_live_guard import require_disposable_instance

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
TRAY_PORT = 8455
# Explicit opt-in for acting on the LIVE tray on :8455 (issue #225) — passed
# to the vendored guard. See the module docstring for what "acting" means
# here (read-only, live_safe-only; never a kill).
_LIVE_ENV = "WR_E2E_LIVE"

# Env-aware wait budget (issue #64). The hosted Windows runner is markedly
# slower than a local dev box, and the WebKit/iPhone projection is the
# notoriously flaky leg (it wedged PR #62 for 11+ min). Every browser wait
# budget is multiplied by WR_E2E_TIMEOUT_SCALE so CI can buy headroom
# (e2e.yml sets it >1) while local runs keep Playwright's native budgets
# (default scale 1.0). One source of truth for the multiplier.
_TIMEOUT_SCALE_ENV = "WR_E2E_TIMEOUT_SCALE"
# Playwright's native default action/navigation budget. Capping it explicitly
# (rather than leaving it implicit) lets a wedged WebKit interaction fail at a
# deterministic, scaled deadline that rerunfailures can retry — instead of
# hanging until the CI job's 30-min cap.
_DEFAULT_TIMEOUT_MS = 30_000


def _timeout_scale() -> float:
    try:
        scale = float(os.environ.get(_TIMEOUT_SCALE_ENV, "1"))
    except ValueError:
        return 1.0
    return scale if scale > 0 else 1.0


def _reachable(port: int, *, timeout: float = 0.3) -> bool:
    url = f"http://127.0.0.1:{port}/healthz"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _autoboot() -> bool:
    return os.environ.get("WR_E2E_AUTOBOOT") == "1"


_E2E_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip the e2e suite unless we can (or already) serve the webapp.

    A sub-conftest hook still receives every collected item, so scope the skip
    to tests under this directory — never touch the offline unit suite.

    Under the live-tray path (autoboot off, tray up), also allowlist to
    ``@pytest.mark.live_safe`` tests only (issue #225): an unmocked test
    could hit real WhatsApp/Gmail connectors or mutate real chat state if
    pointed at the live tray, so exclusion is the default rather than
    something anyone has to remember to add per test.

    Also give the WebKit/iPhone projection a bounded retry (issue #64): it is
    the known-flaky leg on the hosted runner, so a one-off slow round-trip
    self-heals rather than red-lighting an unrelated PR — while the Chromium
    projection stays loud (a Chromium failure is a real product bug). Needs
    pytest-rerunfailures (requirements-dev).
    """
    autoboot = _autoboot()
    tray_up = _reachable(TRAY_PORT)
    serve = autoboot or tray_up
    live_path = tray_up and not autoboot
    skip_offline = pytest.mark.skip(
        reason="e2e disabled: set WR_E2E_AUTOBOOT=1 or run a tray on :8455"
    )
    skip_not_live_safe = pytest.mark.skip(
        reason="e2e live-tray path only runs @pytest.mark.live_safe tests "
        "(issue #225) — this test is unmocked and could touch real data"
    )
    flaky = pytest.mark.flaky(reruns=2, reruns_delay=1)
    for item in items:
        if _E2E_DIR not in Path(item.fspath).parents:
            continue
        if not serve:
            item.add_marker(skip_offline)
        elif live_path and item.get_closest_marker("live_safe") is None:
            item.add_marker(skip_not_live_safe)
        if "[webkit" in item.nodeid:
            item.add_marker(flaky)


def _seed_e2e_db(db_path: Path) -> None:
    """Seed the throwaway e2e DB with SANITIZED fixtures only.

    The Chats tab needs rows to toggle / open a history overlay for. Per the
    project's hard privacy rule we use generic names ("Class 4A Group", …) and
    invented text — never real WhatsApp data. One chat starts monitored, one
    starts discovered so the toggle path has something to flip.
    """
    from src.db import store
    from src.models import ChatRecord, MessageRecord

    conn = store.connect(db_path)
    try:
        mon = store.upsert_chat(
            conn,
            ChatRecord(source_chat_id="e2e-g1", display_name="Class 4A Group", chat_type="group"),
        )
        store.upsert_chat(
            conn,
            ChatRecord(
                source_chat_id="e2e-g2", display_name="School Parents Group", chat_type="group"
            ),
        )
        store.set_chat_status(conn, mon, "monitored")
        for i in range(3):
            store.insert_message(
                conn,
                mon,
                MessageRecord(
                    source_message_id=f"e2e-a{i}",
                    message_timestamp=f"2026-06-01T10:0{i}:00+00:00",
                    text=f"sample message {i}",
                    sender_label="Parent",
                ),
            )
        # One long message so the on-demand Summarize control (#86) has something
        # to attach to (it only shows past SUMMARIZE_MIN_CHARS). Sanitized/invented
        # content per the privacy rule — never real WhatsApp data.
        store.insert_message(
            conn,
            mon,
            MessageRecord(
                source_message_id="e2e-long",
                message_timestamp="2026-06-01T10:05:00+00:00",
                text=(
                    "Reminder for the class trip on Friday: please send the signed "
                    "permission form and 12 euros with your child by Thursday morning. "
                    "Bring a packed lunch, a refillable water bottle, comfortable shoes, "
                    "and a light raincoat. The coach leaves at 8:30 sharp and returns "
                    "around 16:00. Let me know if anyone can volunteer to help supervise."
                ),
                sender_label="Teacher",
            ),
        )
        gmail = store.upsert_chat(
            conn,
            ChatRecord(
                source_chat_id="e2e-mail",
                display_name="School Updates",
                chat_type="email",
                source="gmail",
            ),
        )
        store.set_chat_status(conn, gmail, "monitored")
        store.insert_message(
            conn,
            gmail,
            MessageRecord(
                source_message_id="e2e-mail-1",
                message_timestamp="2026-06-02T09:00:00+00:00",
                text="The activity deadline is Friday.",
                sender_label="School Office",
                message_type="email",
                raw={
                    "thread_id": "e2e-thread-1",
                    "headers": {"Subject": "Activity schedule"},
                },
            ),
        )
        # A DISCOVERED Gmail sender (#166): keyed "sender:<addr>" and left at the
        # default 'discovered' status so the Messages tab has a promotable sender and
        # the history overlay's sender chip has an address to show. Sanitized only.
        discovered = store.upsert_chat(
            conn,
            ChatRecord(
                source_chat_id="sender:newsletter@example.com",
                display_name="Class Newsletter",
                chat_type="email",
                source="gmail",
            ),
        )
        store.insert_message(
            conn,
            discovered,
            MessageRecord(
                source_message_id="e2e-news-1",
                message_timestamp="2026-06-03T09:00:00+00:00",
                text="This week's class newsletter is attached.",
                sender_label="Class Newsletter",
                message_type="email",
                raw={
                    "thread_id": "e2e-thread-2",
                    "headers": {"Subject": "Weekly update"},
                },
            ),
        )
    finally:
        conn.close()


@pytest.fixture(scope="session")
def base_url() -> Iterator[str]:
    if not _autoboot():
        if not _reachable(TRAY_PORT):
            pytest.skip("e2e disabled (no autoboot, no tray on :8455)")
            return
        # Vendored guard (issue #225/#191/#194/#197): refuses via pytest.exit
        # if the live tray is occupied and WR_E2E_LIVE isn't set. This repo's
        # caller-side choice on an opt-in hit is to *adopt* the tray
        # read-only (never reclaim/kill — see module docstring); collection
        # already restricted the run to @pytest.mark.live_safe tests.
        require_disposable_instance(TRAY_PORT, _LIVE_ENV)
        logger.info(
            "driving LIVE tray at http://127.0.0.1:%s (%s=1)", TRAY_PORT, _LIVE_ENV
        )
        yield f"http://127.0.0.1:{TRAY_PORT}"
        return

    port = _free_port()
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Execution-tab runs spawn the CLI; pin the offline stub classifier + no
    # notifier so a dry-run scan stays fast and never touches the network/hub.
    env["WR_CLASSIFIER"] = "stub"
    env["WR_CONNECTOR"] = "fixture"
    env["WR_NOTIFIER"] = "none"
    env["WR_SOURCES"] = "whatsapp"
    # Never read the developer's real WhatsApp data: point the autobooted app at
    # a throwaway empty DB (Dashboard metrics render as zeros). Honors the
    # project's hard privacy rule — e2e runs only against sanitized/empty state.
    db_dir = tempfile.mkdtemp(prefix="wr-e2e-")
    db_path = Path(db_dir) / "e2e.sqlite3"
    env["WR_DB_PATH"] = str(db_path)
    # Family rules normally live in the developer's ignored local config. Give
    # the e2e webapp an explicit sanitized config layer so both reads and any
    # accidental Family-tab saves stay inside this disposable directory.
    local_config_path = Path(db_dir) / "e2e-local.json"
    local_config_path.write_text(
        '{"family": {"enabled": false, "home_address": "", '
        '"kids_home_time": "17:30", "responsible_by_weekday": {}, '
        '"childcare_windows": []}}',
        encoding="utf-8",
    )
    env["WR_LOCAL_CONFIG_PATH"] = str(local_config_path)
    # Never read the developer's real WhatsApp buffer: point the sidecar status /
    # QR routes at an empty throwaway dir (renders as a 'stopped' connection).
    env["WR_LINKED_DEVICE_DIR"] = str(Path(db_dir) / "linked_device")
    _seed_e2e_db(db_path)
    # Capture the autobooted webapp's output to a gitignored log file (the
    # fleet convention, matching voice-transcriber / app-launcher) rather than
    # discarding it: CI uploads this on failure so a runner-only e2e break is
    # diagnosable from the run page without a local repro.
    log_dir = ROOT / "webapp"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = (log_dir / "e2e-autoboot-webapp.log").open(
        "w", encoding="utf-8", errors="replace"
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.webapp.server:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=str(ROOT),
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        # Scale the readiness budget too (issue #64): a cold hosted runner can
        # take longer than 20s to import + bind uvicorn. Local scale=1 keeps 20s.
        ready_budget = 20 * _timeout_scale()
        deadline = time.time() + ready_budget
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("uvicorn exited before becoming ready")
            if _reachable(port):
                break
            time.sleep(0.3)
        else:
            raise RuntimeError(
                f"webapp did not become ready within {ready_budget:.0f}s"
            )
        logger.info(
            "driving disposable autoboot webapp at http://127.0.0.1:%s", port
        )
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        log.close()
        shutil.rmtree(db_dir, ignore_errors=True)


@pytest.fixture(scope="session")
def scaled() -> Callable[[float], int]:
    """Return ``scale(base_ms)`` → env-scaled milliseconds (issue #64).

    Use it on every explicit Playwright wait budget so a slow hosted runner
    gets headroom (CI sets WR_E2E_TIMEOUT_SCALE>1) while local runs keep the
    base value (default scale 1.0).
    """
    factor = _timeout_scale()

    def _scale(base_ms: float) -> int:
        return int(base_ms * factor)

    return _scale


@pytest.fixture(autouse=True)
def _scaled_page_timeouts(request: pytest.FixtureRequest) -> None:
    """Cap + scale Playwright's default action/navigation budget (issue #64).

    Only configures tests that actually use a ``page`` (the cache-busting
    server-side checks drive ``requests`` and never launch a browser). Capping
    the default budget means a wedged WebKit interaction fails at a
    deterministic, scaled deadline that the per-test rerun can retry — instead
    of hanging until the CI job's 30-min cap. Local scale=1 keeps Playwright's
    native 30s budget, so a green local run is unchanged.
    """
    if "page" not in request.fixturenames:
        return
    budget = int(_DEFAULT_TIMEOUT_MS * _timeout_scale())
    page = request.getfixturevalue("page")
    page.set_default_timeout(budget)
    page.set_default_navigation_timeout(budget)
