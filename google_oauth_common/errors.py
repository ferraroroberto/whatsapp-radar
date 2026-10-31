"""Privacy-safe error strings shared by the portable Google packages."""

from __future__ import annotations


def safe_http_error_detail(exc: Exception, api_name: str) -> str:
    """Map a Google API failure to a string that never echoes message or event content.

    Only the HTTP status class, the exception type and a missing-credentials path
    are exposed; ``api_name`` ("Gmail", "Calendar") names the API in the text.
    """
    if isinstance(exc, TimeoutError):
        # Distinct from a generic failure: the transport-level timeout bounds
        # what would otherwise be an unbounded hang.
        return f"{api_name} API request timed out — network stalled or Google unreachable"
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status == 401:
        return "OAuth token is invalid or expired"
    if status == 403:
        return f"{api_name} API permission or quota denied"
    if status == 429:
        return f"{api_name} API quota exceeded"
    if isinstance(exc, FileNotFoundError):
        return str(exc)
    return f"{api_name} API request failed ({type(exc).__name__})"
