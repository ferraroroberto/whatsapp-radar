"""System-tray launcher — owns the webapp + optional Cloudflare tunnel.

Phone-first means there's no desktop UI to surface; the tray exists so
``tray.bat`` brings the webapp up alongside Windows login without keeping a
console window open.

Menu:
    Open               — open the local URL in the default browser
    Copy local URL     — clipboard the local URL (+ ?token= when set)
    Copy Tailscale URL — clipboard the tailnet URL (+ ?token=)
    Copy Cloudflare URL— clipboard the public URL (when a tunnel is configured)
    Restart webapp     — stop + start so a new pull is picked up
    Enroll device      — open a one-time passkey enrollment window
    Status             — popup with webapp state
    Quit               — stop the webapp and exit

No session-host (this app has no terminal). cloudflared only starts when
``webapp/cloudflared.yml`` exists; otherwise the tray is Tailscale-only.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from app.tray import cloudflared_proc
from app.tray.cloudflared_proc import TUNNEL_URL_FILE
from app.tray.single_instance import SingleInstance
from app.webapp.manager import WebappManager, WebappManagerConfig
from src.config import load_config
from src.connector import sidecar
from src.webapp_config import append_auth_token, load_webapp_config

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
TUNNEL_CONFIG_PATH = PROJECT_ROOT / "webapp" / "cloudflared.yml"
TS_DEBUG_LOG = PROJECT_ROOT / "webapp" / "tailscale_debug.log"


def _build_icon() -> object:
    """Lazy import Pillow so plain CLI use doesn't drag it in."""
    from PIL import Image

    tray_ico = PROJECT_ROOT / "assets" / "tray" / "whatsapp-radar.ico"
    if tray_ico.exists():
        return Image.open(tray_ico)
    icon_path = PROJECT_ROOT / "app" / "webapp" / "static" / "icon-512.png"
    if icon_path.exists():
        return Image.open(icon_path)
    return Image.new("RGB", (32, 32), (37, 211, 102))


def _clipboard_copy(text: str) -> bool:
    """Best-effort Windows clipboard. Returns True on success."""
    if sys.platform == "win32":
        try:
            p = subprocess.run(["clip"], input=text, text=True, check=False, encoding="utf-8")
            return p.returncode == 0
        except OSError as exc:
            logger.debug(f"clip failed: {exc}")
    return False


def _tailscale_binary() -> str | None:
    found = shutil.which("tailscale")
    if found:
        return found
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Tailscale" / "tailscale.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Tailscale" / "tailscale.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _run_tailscale(binary: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(
        [binary, *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=12,
        check=False,
        creationflags=creationflags,
    )


def _tailscale_hostname() -> str | None:
    """Return this machine's tailnet FQDN (or 100.x IP), or None."""
    binary = _tailscale_binary()
    if binary is None:
        return None
    try:
        result = _run_tailscale(binary, ["status", "--self=true", "--peers=false", "--json"])
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        try:
            data = json.loads(result.stdout)
            dns = ((data.get("Self") or {}).get("DNSName") or "").rstrip(".")
            if dns:
                return dns
        except ValueError:
            pass
    try:
        ip_res = _run_tailscale(binary, ["ip", "-4"])
        if ip_res.returncode == 0:
            lines = (ip_res.stdout or "").strip().splitlines()
            if lines:
                return lines[0].strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _notify(title: str, message: str) -> None:
    """Show a Windows toast when available; log otherwise."""
    logger.info(f"🔔 {title}: {message}")
    if sys.platform != "win32":
        return
    try:
        from winotify import Notification

        Notification(app_id="WhatsApp Radar", title=title, msg=message).show()
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"winotify failed: {exc}")


def run_tray() -> int:
    """Run the tray icon. Returns when the user picks Quit."""
    try:
        import pystray
        from pystray import Menu, MenuItem
    except ImportError as exc:
        logger.error(f"❌ pystray not installed ({exc}); pip install -r requirements.txt")
        return 1

    # In-process single-instance guard (project-scaffolding#39): the tray.bat CIM
    # pre-check can let two near-simultaneous launches through, so the guarantee
    # must live in the process. Held for the tray's lifetime; the OS frees the
    # named mutex on exit. `instance` is intentionally kept referenced below.
    instance = SingleInstance(r"Global\whatsapp-radar-tray")
    if not instance.acquired:
        logger.info("ℹ️  Another whatsapp-radar tray is already running; exiting.")
        return 0

    wcfg = load_webapp_config()
    cfg = load_config()
    manager = WebappManager(
        WebappManagerConfig(enabled=wcfg.enabled, host=wcfg.host, port=wcfg.port)
    )

    tunnel_hostname = cloudflared_proc.read_hostname(TUNNEL_CONFIG_PATH)
    tunnel_state: dict[str, subprocess.Popen[bytes] | None] = {"proc": None}
    starter_error: dict[str, Exception | None] = {"exc": None}

    def _start() -> None:
        try:
            manager.start(wait=True)
            _notify("WhatsApp Radar ready", manager.base_url)
        except Exception as exc:  # noqa: BLE001
            starter_error["exc"] = exc
            logger.error(f"❌ webapp start failed: {exc}")
            _notify("WhatsApp Radar start failed", str(exc))

    threading.Thread(target=_start, daemon=True).start()

    def _start_tunnel() -> None:
        if tunnel_hostname is None:
            return
        bin_path = shutil.which("cloudflared")
        if bin_path is None:
            logger.warning("⚠️  cloudflared not on PATH — public URL won't be reachable.")
            _notify("Cloudflare tunnel", "cloudflared not on PATH — install via winget")
            return
        cmd = [bin_path, "tunnel", "--config", str(TUNNEL_CONFIG_PATH), "run"]
        kw: dict[str, object] = dict(
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if sys.platform == "win32":
            kw["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
            )
        try:
            proc = subprocess.Popen(cmd, **kw)  # type: ignore[call-overload]
        except OSError as exc:
            logger.warning(f"⚠️  cloudflared failed to launch: {exc}")
            _notify("Cloudflare tunnel", f"Failed to start: {exc}")
            return
        tunnel_state["proc"] = proc
        logger.info(f"🌍 Cloudflare tunnel started → https://{tunnel_hostname} (pid={proc.pid})")

        token = (load_webapp_config().auth_token or "").strip()
        cloudflared_proc.persist_tunnel_url(tunnel_hostname, token)

    def _stop_tunnel() -> None:
        proc = tunnel_state.get("proc")
        tunnel_state["proc"] = None
        if proc is None:
            return
        cloudflared_proc.stop_proc(proc, "cloudflared")
        cloudflared_proc.cleanup_url_file()

    if tunnel_hostname is not None:
        threading.Thread(target=_start_tunnel, daemon=True).start()

    # Keep-alive supervisor (#73): while the tray is open, keep the read-only
    # WhatsApp sidecar running so the buffer stays warm and a scan never reads a
    # cold/half-loaded source. Linked-device only (the fixture has no process),
    # and gated on the same self-heal flag as the scan preflight.
    supervisor_stop = threading.Event()
    last_supervisor_state: dict[str, str | None] = {"state": None}

    def _on_supervise(result: dict[str, object]) -> None:
        state = str(result.get("state"))
        action = str(result.get("action"))
        # Toast only on the *transition* into needs_qr — a human must re-pair, and
        # relaunching can't help, so nag once rather than every 90 s tick.
        if action == sidecar.ACTION_NEEDS_QR and last_supervisor_state["state"] != state:
            _notify(
                "WhatsApp Radar: re-pair needed",
                "The linked device was logged out — open the webapp to scan a new QR.",
            )
        last_supervisor_state["state"] = state

    def _run_supervisor() -> None:
        try:
            sidecar.run_supervisor(
                cfg.linked_device_dir, supervisor_stop, on_tick=_on_supervise
            )
        except Exception as exc:  # noqa: BLE001 — a supervisor crash must not kill the tray
            logger.error(f"❌ sidecar supervisor stopped: {exc}")

    if cfg.connector == "linked_device" and cfg.sidecar_autostart:
        logger.info("🩺 sidecar keep-alive supervisor starting")
        threading.Thread(target=_run_supervisor, daemon=True).start()

    def copy_local(icon: object, item: object) -> None:
        url = append_auth_token(manager.base_url, load_webapp_config().auth_token)
        _notify("Copied local URL" if _clipboard_copy(url) else "Local URL", url)

    def copy_tailscale(icon: object, item: object) -> None:
        host = _tailscale_hostname()
        if not host:
            _notify("Tailscale not available", "Couldn't resolve a tailnet address.")
            return
        url = append_auth_token(
            f"http://{host}:{manager.config.port}", load_webapp_config().auth_token
        )
        _notify("Copied Tailscale URL" if _clipboard_copy(url) else "Tailscale URL", url)

    def copy_tunnel(icon: object, item: object) -> None:
        if not TUNNEL_URL_FILE.exists():
            _notify("No tunnel URL yet", "Configure webapp/cloudflared.yml to enable it.")
            return
        try:
            url = TUNNEL_URL_FILE.read_text(encoding="utf-8").strip()
        except OSError as exc:
            _notify("Tunnel URL read failed", str(exc))
            return
        if url:
            _notify("Copied Cloudflare URL" if _clipboard_copy(url) else "Cloudflare URL", url)

    def restart_webapp(icon: object, item: object) -> None:
        def _do() -> None:
            try:
                _notify("WhatsApp Radar", "Restarting webapp…")
                manager.restart(wait=True)
                _notify("WhatsApp Radar restarted", manager.base_url)
            except Exception as exc:  # noqa: BLE001
                logger.error(f"❌ webapp restart failed: {exc}")
                _notify("Restart failed", str(exc))

        threading.Thread(target=_do, daemon=True).start()

    def enroll_device(icon: object, item: object) -> None:
        def _do() -> None:
            url = f"http://127.0.0.1:{manager.config.port}/api/webauthn/enroll/window"
            try:
                import requests

                resp = requests.post(url, json={"seconds": 300}, timeout=5)
                if resp.status_code == 200:
                    _notify(
                        "Passkey enrollment",
                        "5-minute window open — enrol your device from Settings now.",
                    )
                else:
                    _notify("Passkey enrollment failed", f"HTTP {resp.status_code}")
            except Exception as exc:  # noqa: BLE001
                _notify("Passkey enrollment failed", str(exc))

        threading.Thread(target=_do, daemon=True).start()

    def show_status(icon: object, item: object) -> None:
        s = manager.status()
        _notify("WhatsApp Radar status", f"{s.detail} · {s.base_url}")

    def open_local(icon: object, item: object) -> None:
        webbrowser.open(manager.base_url)

    def quit_app(icon: object, item: object) -> None:
        logger.info("👋 Tray quit requested")
        supervisor_stop.set()
        _stop_tunnel()
        try:
            manager.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"⚠️  stop failed: {exc}")
        instance.release()
        icon.stop()  # type: ignore[attr-defined]

    menu = Menu(
        MenuItem("📡 Open WhatsApp Radar", open_local, default=True),
        MenuItem("📋 Copy local URL", copy_local),
        MenuItem("📋 Copy Tailscale URL", copy_tailscale),
        MenuItem("📋 Copy Cloudflare URL", copy_tunnel),
        Menu.SEPARATOR,
        MenuItem("🔄 Restart webapp", restart_webapp),
        MenuItem("🔐 Enroll device (5 min)", enroll_device),
        MenuItem("ℹ️ Status", show_status),
        Menu.SEPARATOR,
        MenuItem("🚪 Quit", quit_app),
    )

    icon = pystray.Icon("whatsapp-radar", icon=_build_icon(), title="WhatsApp Radar", menu=menu)
    icon.run()
    if starter_error["exc"] is not None:
        return 1
    return 0
