"""Connector boundary.

The connector is the highest-risk, most replaceable dependency, so it sits
behind a narrow read-only interface. No implementation may expose a send,
reaction, read-receipt, or group-admin operation.
"""

from .base import ConnectorStatus, MessageConnector
from .fixture import FixtureConnector

__all__ = ["ConnectorStatus", "MessageConnector", "FixtureConnector"]
