"""Construct a connector from config.

Kept out of the CLI so both the CLI and the scan pipeline build connectors
through one code path. ``fixture`` is the deterministic, offline default;
``linked_device`` reads the read-only Node/Baileys sidecar buffer.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import Config
from src.connector.base import MessageConnector
from src.connector.fixture import FixtureConnector
from src.connector.gmail import GmailConnector
from src.connector.linked_device import LinkedDeviceConnector


@dataclass(frozen=True)
class ConnectorBinding:
    """One logical source paired with its read-only connector instance."""

    source: str
    connector: MessageConnector


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


def build_connectors(config: Config) -> list[ConnectorBinding]:
    """Build one connector for every enabled logical source.

    WhatsApp keeps using the legacy ``config.connector`` selector. Other
    sources register here in their own issue; naming an unavailable source is a
    loud configuration error rather than silently skipping it.
    """
    bindings: list[ConnectorBinding] = []
    for source in config.sources:
        if source == "whatsapp":
            bindings.append(ConnectorBinding(source="whatsapp", connector=build_connector(config)))
            continue
        if source == "gmail":
            bindings.append(
                ConnectorBinding(source="gmail", connector=GmailConnector(config.gmail))
            )
            continue
        raise ValueError(f"source {source!r} is enabled but no connector is available")
    return bindings
