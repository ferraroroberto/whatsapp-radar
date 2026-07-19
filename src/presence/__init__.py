"""Live phone-location lookup via home-automation's presence API (issue #169).

Read-only cross-repo dependency: this package asks home-automation "where is the
responsible parent right now" so the family traffic check can route from the
person's actual phone position instead of guessing the origin from calendar text.
The one seam is :func:`src.presence.client.get_location`; everything it returns is
a typed result (a fresh location, or an explicit "unavailable" reason) so callers
degrade cleanly to the calendar-inference fallback when home-automation is down.

Privacy: raw coordinates never leave in-memory transient use (they feed one
outbound Routes request and nothing else). Persisted values are derived only —
distance, freshness age — never the lat/lon themselves.
"""

from src.presence.client import (
    PresenceLocation,
    PresenceResult,
    PresenceUnavailable,
    get_location,
)

__all__ = [
    "PresenceLocation",
    "PresenceResult",
    "PresenceUnavailable",
    "get_location",
]
