"""Read-only Google Calendar credentials + the household calendars (#160)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CalendarAccount:
    """One household calendar and the person it belongs to."""

    calendar_id: str  # the calendar id (an email address)
    person: str  # canonical person key, e.g. "roberto" / "ana"
    label: str = ""  # optional display label


@dataclass(frozen=True)
class CalendarConfig:
    """Read-only Google Calendar credentials + the household calendars (#160).

    ``write_token_path`` (#217) is a separate credential for the write-scope
    adapter (``calendar_write``) — Google Calendar OAuth scopes cannot be
    upgraded in place on an existing token, so event creation needs its own
    grant and its own token file, distinct from ``token_path`` above.
    """

    credentials_path: Path = Path("auth/calendar/credentials.json")
    token_path: Path = Path("auth/calendar/token.json")
    write_token_path: Path = Path("auth/calendar/write_token.json")
    accounts: tuple[CalendarAccount, ...] = ()
    # Duplicate `calendar_id` entries collapsed at parse time (#273): one entry
    # per dropped duplicate, named by its *label* (falling back to its person)
    # — never the raw calendar id, so this stays safe to render on the Family
    # tab. Empty on every config that has no such collision.
    collapsed_duplicate_labels: tuple[str, ...] = ()


def parse(raw: dict[str, Any], root: Path) -> CalendarConfig:
    creds = Path(
        os.environ.get(
            "WR_CALENDAR_CREDENTIALS_PATH",
            raw.get("credentials_path", "auth/calendar/credentials.json"),
        )
    )
    if not creds.is_absolute():
        creds = root / creds
    token = Path(
        os.environ.get("WR_CALENDAR_TOKEN_PATH", raw.get("token_path", "auth/calendar/token.json"))
    )
    if not token.is_absolute():
        token = root / token
    write_token = Path(
        os.environ.get(
            "WR_CALENDAR_WRITE_TOKEN_PATH",
            raw.get("write_token_path", "auth/calendar/write_token.json"),
        )
    )
    if not write_token.is_absolute():
        write_token = root / write_token
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
    """Keep the first ``calendar.accounts`` entry for a given calendar id (#273).

    A calendar belongs to one person by construction, so two entries sharing a
    ``calendar_id`` is not a configuration the rest of the family logic (in
    particular the travel-blocks reconcile, whose ``leg_key`` is keyed on
    ``calendar_id``) can represent coherently. Refusing to start would be a
    worse failure for a household whose ``config/local.json`` already contains
    the duplicate by mistake, so this collapses to the first entry and reports
    the drop loudly instead — never silently, and never by refusing to boot.

    Do not "fix" the collision downstream by folding ``person`` into
    ``leg_key`` instead — that would legitimise two people owning one calendar
    and push the same ambiguity into the block markers it writes.
    """
    kept: dict[str, CalendarAccount] = {}
    collapsed: list[str] = []
    for account in accounts:
        first = kept.get(account.calendar_id)
        if first is None:
            kept[account.calendar_id] = account
            continue
        kept_label = first.label or first.person
        dropped_label = account.label or account.person
        collapsed.append(dropped_label)
        logger.warning(
            "⚠️ calendar.accounts: %s and %s share one calendar id — keeping %s and "
            "collapsing %s, to stop the travel-blocks sweep churning that calendar's "
            "blocks forever (#273)",
            kept_label,
            dropped_label,
            kept_label,
            dropped_label,
        )
    return tuple(kept.values()), tuple(collapsed)
