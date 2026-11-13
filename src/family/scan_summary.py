"""Reading a stored ``calendar-scan`` summary: the one place its keys are known.

Rows written before the rename carry the missing-location list under
``unknown_locations``. Every surface that shows the scan (Home card, Family
header, Activity row chips and funnel cells, run sheet) reads it through here,
so a future key change is patched once and the browser never has to know the
legacy spelling.
"""

from __future__ import annotations

from typing import Any

#: The pre-rename name of ``missing_locations``; old rows keep it.
LEGACY_MISSING_KEY = "unknown_locations"


def normalize_scan_summary(result: Any) -> Any:
    """The stored summary with the missing-location list under ``missing_locations``.

    Anything that is not a dict (a null column, a non-object payload) comes
    back unchanged, so callers can pass whatever ``loads_json_column`` gave
    them. The input is never mutated.
    """
    if not isinstance(result, dict) or LEGACY_MISSING_KEY not in result:
        return result
    out = dict(result)
    legacy = out.pop(LEGACY_MISSING_KEY)
    out["missing_locations"] = out.get("missing_locations") or legacy or []
    return out


def scan_counts(result: Any) -> tuple[int, int]:
    """``(conflicts, missing_locations)`` counts of a stored calendar-scan summary."""
    summary = normalize_scan_summary(result)
    if not isinstance(summary, dict):
        return 0, 0
    return len(summary.get("conflicts") or []), len(summary.get("missing_locations") or [])
