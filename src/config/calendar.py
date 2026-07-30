"""Read-only Google Calendar credentials + the household calendars (#160)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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
    accounts = tuple(
        CalendarAccount(
            calendar_id=str(item.get("calendar_id", "")).strip(),
            person=str(item.get("person", "")).strip().lower(),
            label=str(item.get("label") or item.get("person") or "").strip(),
        )
        for item in raw.get("accounts", [])
        if isinstance(item, dict) and str(item.get("calendar_id", "")).strip()
    )
    return CalendarConfig(
        credentials_path=creds, token_path=token, write_token_path=write_token, accounts=accounts
    )
