"""Connector liveness preflight — the first step of any live run.

A live ``scan`` / ``resync`` must never proceed against a dead message source.
Before #29 the pipeline called ``connector.connect()`` and *ignored* the result,
so a scan against a stopped/stale sidecar read the stale buffer, found no new
messages, advanced cursors over a no-op, and reported success — a scheduled job
would look green while completely blind.

This module is the gate. :func:`ensure_connected` is the hard check any caller
can use; :func:`preflight` adds the self-heal: for the linked-device connector it
will (optionally) relaunch the sidecar once and re-check before giving up, so a
process that merely died is recovered automatically as the first step of the run.
If the source still isn't live it raises :class:`ConnectorOffline`, which the
caller turns into a loud, non-zero-exit failure (never a silent green run).
"""

from __future__ import annotations

from collections.abc import Callable

from src.config import Config
from src.connector.base import ConnectorStatus, MessageConnector
from src.connector.sidecar import ensure_running, wait_for_settled

Progress = Callable[[str], None]


class ConnectorOffline(RuntimeError):
    """Raised when the message source is not live, so a run must abort loudly."""

    def __init__(self, status: ConnectorStatus) -> None:
        self.status = status
        detail = status.detail or "connector is offline"
        super().__init__(f"{status.name} offline — {detail}")


def _emit(progress: Progress | None, line: str) -> None:
    if progress is not None:
        progress(line)


def ensure_connected(connector: MessageConnector) -> ConnectorStatus:
    """Confirm the connector is live, or raise :class:`ConnectorOffline`.

    Calls ``connect()`` (which also primes connectors that load lazily, like the
    fixture) and treats a non-``connected`` status as a hard stop.
    """
    status = connector.connect()
    if not status.connected:
        raise ConnectorOffline(status)
    return status


def preflight(
    config: Config,
    connector: MessageConnector,
    *,
    source: str = "whatsapp",
    progress: Progress | None = None,
) -> ConnectorStatus:
    """Ensure the source is live before a run, self-healing the sidecar if it can.

    Returns the live :class:`ConnectorStatus`. If the source is offline and (for
    the linked-device connector) ``sidecar_autostart`` is enabled, it relaunches
    the sidecar once and re-checks; a session that still can't connect (e.g. it
    needs a fresh QR) raises :class:`ConnectorOffline`.

    Before returning a live linked-device status it also waits for the buffer to
    *settle* (#73): a (re)connect streams history in asynchronously, so reading
    immediately would under-report — and a live scan advances cursors over what it
    read, permanently skipping the un-synced tail. The gate is a fast no-op once
    the buffer is quiet (the steady state with the tray keep-alive running).
    """
    status = connector.connect()
    if status.connected:
        _settle(config, source, progress)
        return status

    if source == "whatsapp" and config.connector == "linked_device" and config.sidecar_autostart:
        _emit(progress, f"⚠ source offline: {status.detail} — relaunching the sidecar…")
        info = ensure_running(config.linked_device_dir)
        status = connector.connect()
        if status.connected:
            _emit(progress, "✓ sidecar back online")
            _settle(config, source, progress)
            return status
        _emit(progress, f"✗ sidecar still offline ({info.state}: {info.detail})")

    raise ConnectorOffline(status)


def _settle(config: Config, source: str, progress: Progress | None) -> None:
    """Wait for the linked-device buffer to stop growing before it is read.

    Only the linked-device connector has a streaming buffer; every other connector
    (the fixture) loads synchronously and needs no gate. A non-positive
    ``sync_settle_seconds`` disables it entirely.
    """
    if (
        source != "whatsapp"
        or config.connector != "linked_device"
        or config.sync_settle_seconds <= 0
    ):
        return
    _emit(progress, "• waiting for the message buffer to settle…")
    settled = wait_for_settled(
        config.linked_device_dir,
        settle_seconds=config.sync_settle_seconds,
        timeout_seconds=config.sync_settle_timeout,
    )
    if settled:
        _emit(progress, "✓ buffer settled — reading")
    else:
        _emit(progress, "• buffer still active after settle timeout — reading anyway")
