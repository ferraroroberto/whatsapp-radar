"""Construct a connector from config.

Kept out of the CLI so both the CLI and the scan pipeline build connectors
through one code path. ``fixture`` is the deterministic, offline default;
``linked_device`` reads the read-only Node/Baileys sidecar buffer.
"""

from __future__ import annotations

from src.config import Config
from src.connector.base import MessageConnector
from src.connector.fixture import FixtureConnector
from src.connector.linked_device import LinkedDeviceConnector


def build_connector(config: Config) -> MessageConnector:
    """Return a connector for ``config.connector`` ('fixture' | 'linked_device')."""
    if config.connector == "fixture":
        return FixtureConnector()
    if config.connector == "linked_device":
        return LinkedDeviceConnector(config.linked_device_dir)
    raise ValueError(
        f"connector {config.connector!r} is not available "
        "(expected 'fixture' or 'linked_device')"
    )
