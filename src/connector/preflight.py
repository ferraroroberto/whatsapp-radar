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
from src.connector.sidecar import ensure_running

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
    progress: Progress | None = None,
) -> ConnectorStatus:
    """Ensure the source is live before a run, self-healing the sidecar if it can.

    Returns the live :class:`ConnectorStatus`. If the source is offline and (for
    the linked-device connector) ``sidecar_autostart`` is enabled, it relaunches
    the sidecar once and re-checks; a session that still can't connect (e.g. it
    needs a fresh QR) raises :class:`ConnectorOffline`.
    """
    status = connector.connect()
    if status.connected:
        return status

    if config.connector == "linked_device" and config.sidecar_autostart:
        _emit(progress, f"⚠ source offline: {status.detail} — relaunching the sidecar…")
        info = ensure_running(config.linked_device_dir)
        status = connector.connect()
        if status.connected:
            _emit(progress, "✓ sidecar back online")
            return status
        _emit(progress, f"✗ sidecar still offline ({info.state}: {info.detail})")

    raise ConnectorOffline(status)
