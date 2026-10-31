"""Read-only Google Calendar credentials + the household calendars."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config._shared import rooted_path

logger = logging.getLogger(__name__)

# Distinct collapsed pairs already warned about this process:
# `load_config()` is uncached and runs on essentially every
# webapp request, so without this a household carrying the duplicate would
# get a WARNING line on every poll instead of once. Keyed on the casefolded
# calendar id plus both labels, so a config edit that changes which entry
# collides (or its label) warns again rather than staying silent forever.
_WARNED_DUPLICATE_CALENDARS: set[tuple[str, str, str]] = set()


@dataclass(frozen=True)
class CalendarAccount:
    """One household calendar and the person it belongs to."""

    calendar_id: str  # the calendar id (an email address)
    person: str  # canonical person key, e.g. "roberto" / "ana"
    label: str = ""  # optional display label


@dataclass(frozen=True)
class CalendarConfig:
    """Read-only Google Calendar credentials + the household calendars.

    ``write_token_path`` is a separate credential for the write-scope
    adapter (``calendar_write``) — Google Calendar OAuth scopes cannot be
    upgraded in place on an existing token, so event creation needs its own
    grant and its own token file, distinct from ``token_path`` above.
    """

    credentials_path: Path = Path("auth/calendar/credentials.json")
    token_path: Path = Path("auth/calendar/token.json")
    write_token_path: Path = Path("auth/calendar/write_token.json")
    accounts: tuple[CalendarAccount, ...] = ()
    # Duplicate `calendar_id` entries collapsed at parse time: one entry
    # per dropped duplicate, named by its *label* (falling back to its person)
    # — never the raw calendar id, so this stays safe to render on the Family
    # tab. Empty on every config that has no such collision.
    collapsed_duplicate_labels: tuple[str, ...] = ()


def parse(raw: dict[str, Any], root: Path) -> CalendarConfig:
    defaults = CalendarConfig()
    creds = rooted_path(
        "WR_CALENDAR_CREDENTIALS_PATH", raw, "credentials_path", defaults.credentials_path, root
    )
    token = rooted_path("WR_CALENDAR_TOKEN_PATH", raw, "token_path", defaults.token_path, root)
    write_token = rooted_path(
        "WR_CALENDAR_WRITE_TOKEN_PATH", raw, "write_token_path", defaults.write_token_path, root
    )
    raw_accounts = [
        CalendarAccount(
            calendar_id=str(item.get("calendar_id", "")).strip(),
            person=str(item.get("person", "")).strip().lower(),
            label=str(item.get("label") or item.get("person") or "").strip(),
        )
        for item in raw.get("accounts", [])
        if isinstance(item, dict) and str(item.get("calendar_id", "")).strip()
    ]
    accounts, collapsed = _collapse_duplicate_calendar_ids(raw_accounts)
    return CalendarConfig(
        credentials_path=creds,
        token_path=token,
        write_token_path=write_token,
        accounts=accounts,
        collapsed_duplicate_labels=collapsed,
    )


def _collapse_duplicate_calendar_ids(
    accounts: list[CalendarAccount],
) -> tuple[tuple[CalendarAccount, ...], tuple[str, ...]]:
    """Keep the first ``calendar.accounts`` entry for a given calendar id.

    A calendar belongs to one person by construction, so two entries sharing a
    ``calendar_id`` is not a configuration the rest of the family logic (in
    particular the travel-blocks reconcile, whose ``leg_key`` is keyed on
    ``calendar_id``) can represent coherently. Refusing to start would be a
    worse failure for a household whose ``config/local.json`` already contains
    the duplicate by mistake, so this collapses to the first entry and reports
    the drop loudly instead — never silently, and never by refusing to boot.

    The dedup key is **casefolded** — Google treats calendar ids case-
    insensitively, so ``A@x`` and ``a@x`` are the same collision and must
    collapse too — but the surviving :class:`CalendarAccount` keeps the first
    entry's original spelling; only the lookup key is casefolded.

    Do not "fix" the collision downstream by folding ``person`` into
    ``leg_key`` instead — that would legitimise two people owning one calendar
    and push the same ambiguity into the block markers it writes.
    """
    kept: dict[str, CalendarAccount] = {}
    collapsed: list[str] = []
    for account in accounts:
        dedup_key = account.calendar_id.casefold()
        first = kept.get(dedup_key)
        if first is None:
            kept[dedup_key] = account
            continue
        kept_label = first.label or first.person
        dropped_label = account.label or account.person
        collapsed.append(dropped_label)
        _warn_duplicate_calendar_once(dedup_key, kept_label, dropped_label)
    return tuple(kept.values()), tuple(collapsed)


def _warn_duplicate_calendar_once(dedup_key: str, kept_label: str, dropped_label: str) -> None:
    """Log the collapse once per process for a given (id, kept, dropped) triple.

    ``load_config()`` is uncached and runs on essentially every webapp request
    (the DB dependency, the family/config/execution routers all call it), so
    without this a household carrying the duplicate would get a WARNING line
    on every poll instead of once at the point it matters. Loud is the goal;
    flooded is its own kind of unreadable.
    """
    seen_key = (dedup_key, kept_label, dropped_label)
    if seen_key in _WARNED_DUPLICATE_CALENDARS:
        return
    _WARNED_DUPLICATE_CALENDARS.add(seen_key)
    logger.warning(
        "⚠️ calendar.accounts: %s and %s share one calendar id — keeping %s and "
        "collapsing %s, to stop the travel-blocks sweep churning that calendar's "
        "blocks forever (#273). If %s has no other calendar configured, they also "
        "disappear from the coverage-check roster entirely (neither available nor "
        "away) until the duplicate entry is removed from config",
        kept_label,
        dropped_label,
        kept_label,
        dropped_label,
        dropped_label,
    )
