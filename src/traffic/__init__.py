"""Google Routes API v2 traffic client for the family traffic-jam check (#160).

API-key auth over REST (a different credential path from the Calendar OAuth
flow). Deterministic: returns normal vs. traffic-aware duration, the delay in
minutes, and a coarse ``NORMAL``/``DELAY``/``SIGNIFICANT_DELAY`` status. No LLM.
"""

from src.traffic.routes_client import (
    DELAY_THRESHOLD_MIN,
    SIGNIFICANT_DELAY_THRESHOLD_MIN,
    RouteResult,
    TrafficReadError,
    compute_route,
    delay_status,
)

__all__ = [
    "DELAY_THRESHOLD_MIN",
    "SIGNIFICANT_DELAY_THRESHOLD_MIN",
    "RouteResult",
    "TrafficReadError",
    "compute_route",
    "delay_status",
]
