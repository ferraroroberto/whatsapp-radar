"""Shared constants, fakes and drivers for the travel-block write tests.

Every person, address and calendar id is invented; both calendar clients are
in-memory fakes, so nothing here can reach a real calendar.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.config import (
    CalendarAccount,
    CalendarConfig,
    Config,
    FamilyConfig,
    HubConfig,
    TelegramConfig,
    TrafficConfig,
    TravelBlocksConfig,
)
from src.family import calendar_source, travel_blocks, travel_blocks_write
from src.family.travel_blocks import (
    LEG_OUTBOUND,
    ExistingBlock,
    PlannedLeg,
)
from src.traffic import RouteResult, TrafficReadError

HOME = "1 Example Street, Sample Town"
OFFICE = "3 Example Road, Sample City"
CLINIC = "4 Example Lane, Sample Town"

PERSON = "parent-a"
CALENDAR_ID = "parent-a@example.test"
PERSON_B = "parent-b"
CALENDAR_B = "parent-b@example.test"

DAY = datetime(2026, 7, 20, tzinfo=UTC)
NOW = DAY.replace(hour=6)
TITLE = "🚗 Trayecto"
#: A plan window comfortably containing every fixture leg below, for the *pure*
#: reconcile tests. The listing deliberately reads past where the plan stops
#: (#272), so the diff has to be told which of the two moments it is judging
#: against — a block beyond it is unplanned-for, not orphaned. The sweep tests
#: derive the real boundary from `sweep_horizon()` instead: the true horizon depends
#: on the machine's local midnight, which is not `DAY`'s.
PLAN_END = DAY + timedelta(days=2)


def at_hour(hour: int, minute: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute)


# --------------------------------------------------------------- fixtures & doubles


def make_raw_event(
    eid: str,
    *,
    location: str = OFFICE,
    summary: str = "Appointment",
    start: datetime | None = None,
    hours: int = 1,
) -> dict[str, Any]:
    """A human's calendar event, as the scan's own fetch returns it."""
    start = start or at_hour(9)
    return {
        "id": eid,
        "summary": summary,
        "location": location,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(hours=hours)).isoformat()},
    }


def make_leg(
    *,
    leg: str = LEG_OUTBOUND,
    source_event_id: str = "e1",
    calendar_id: str = CALENDAR_ID,
    person: str = PERSON,
    origin: str = HOME,
    destination: str = OFFICE,
    minutes: int = 20,
    start: datetime | None = None,
    end: datetime | None = None,
) -> PlannedLeg:
    return PlannedLeg(
        person=person,
        calendar_id=calendar_id,
        leg=leg,
        source_event_id=source_event_id,
        origin=origin,
        destination=destination,
        start=start or at_hour(8, 40),
        end=end or at_hour(9),
        source_start=at_hour(9),
        source_end=at_hour(10),
        minutes=minutes,
    )


def make_raw_block(leg: PlannedLeg, *, event_id: str = "blk-1") -> dict[str, Any]:
    """The resource this app writes for ``leg`` — marker and all — read back."""
    return {
        "id": event_id,
        "summary": TITLE,
        "location": leg.destination,
        "start": {"dateTime": leg.start.isoformat()},
        "end": {"dateTime": leg.end.isoformat()},
        "extendedProperties": {"private": travel_blocks.block_marker(leg)},
    }


def make_block(leg: PlannedLeg, *, event_id: str = "blk-1") -> ExistingBlock:
    parsed = travel_blocks.parse_existing_block(
        make_raw_block(leg, event_id=event_id), calendar_id=leg.calendar_id
    )
    assert parsed is not None
    return parsed


def within_window(
    event: dict[str, Any], *, time_min: datetime, time_max: datetime
) -> bool:
    """Google's own `events.list` window semantics, which #272 turns on.

    ``timeMin`` is an exclusive lower bound on an event's **end**; ``timeMax`` an
    exclusive upper bound on its **start**. The asymmetry is the whole bug: a
    block starting at or after ``timeMax`` is simply not returned, so the
    reconcile could not see the block it had written and re-inserted it every
    sweep. A fake that ignored the window could not reproduce that.
    """
    start = datetime.fromisoformat(event["start"]["dateTime"])
    end = datetime.fromisoformat(event["end"]["dateTime"])
    return end > time_min and start < time_max


class FakeReadClient:
    """Stands in for the Calendar read client; records how it was queried."""

    def __init__(
        self,
        marked: dict[str, list[dict[str, Any]]] | None = None,
        *,
        roles: dict[str, str | None] | None = None,
        list_fails: Sequence[str] = (),
        role_fails: Sequence[str] = (),
    ) -> None:
        self._marked = marked or {}
        self._roles = roles if roles is not None else {}
        self._list_fails = set(list_fails)
        self._role_fails = set(role_fails)
        self.list_kwargs: list[dict[str, Any]] = []
        self.closed = False

    def list_events(
        self,
        *,
        calendar_id: str,
        time_min: datetime,
        time_max: datetime,
        private_extended_property: str | None = None,
    ) -> list[dict[str, Any]]:
        self.list_kwargs.append({
            "calendar_id": calendar_id,
            "time_min": time_min,
            "time_max": time_max,
            "private_extended_property": private_extended_property,
        })
        if calendar_id in self._list_fails:
            raise RuntimeError("calendar listing exploded")
        return [
            event
            for event in self._marked.get(calendar_id, [])
            if within_window(event, time_min=time_min, time_max=time_max)
        ]

    def calendar_access_role(self, calendar_id: str) -> str | None:
        if calendar_id in self._role_fails:
            raise RuntimeError("calendarList unavailable")
        return self._roles.get(calendar_id, "owner")

    def close(self) -> None:
        self.closed = True


class FakeWriteClient:
    """Records inserts and deletes; never talks to Google."""

    def __init__(
        self, *, insert_fails: Sequence[str] = (), delete_fails: Sequence[str] = ()
    ) -> None:
        self.inserted: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[tuple[str, str]] = []
        #: Whether a backup for the event already existed at delete time — the
        #: only honest way to assert the *ordering* of backup and API call.
        self.backup_present_at_delete: list[bool] = []
        self.backup_root: Path | None = None
        self.closed = False
        self._insert_fails = set(insert_fails)
        self._delete_fails = set(delete_fails)

    def insert_event(self, *, calendar_id: str, event: dict[str, Any]) -> dict[str, Any]:
        if calendar_id in self._insert_fails:
            raise RuntimeError("insert rejected")
        self.inserted.append((calendar_id, event))
        return {"id": f"new-{len(self.inserted)}"}

    def delete_event(self, *, calendar_id: str, event_id: str) -> None:
        if self.backup_root is not None:
            self.backup_present_at_delete.append(
                travel_blocks_write.backup_path(
                    self.backup_root, calendar_id=calendar_id, event_id=event_id, now=NOW
                ).is_file()
            )
        if event_id in self._delete_fails:
            raise RuntimeError("delete rejected")
        self.deleted.append((calendar_id, event_id))

    def close(self) -> None:
        self.closed = True


class StubRoutes:
    """A `RouteFn` that answers a fixed duration, or raises like the real client.

    ``fails`` reproduces a Routes outage — a transport error, a non-200 (an
    ``HTTP 429`` rate-limit included) or an empty ``routes`` list all surface as
    :class:`TrafficReadError`.
    """

    def __init__(self, minutes: float = 20, *, fails: bool = False) -> None:
        self._minutes = minutes
        self._fails = fails
        self.calls = 0

    def __call__(
        self,
        origin: str,
        destination: str,
        *,
        api_key: str,
        departure_time: datetime | None = None,
        session: Any = None,
    ) -> RouteResult:
        self.calls += 1
        if self._fails:
            raise TrafficReadError("Routes API returned HTTP 429")
        return RouteResult(normal_s=int(self._minutes * 60), traffic_s=int(self._minutes * 60))


def make_config(
    *,
    enabled: bool = True,
    dry_run: bool = False,
    accounts: tuple[CalendarAccount, ...] = (
        CalendarAccount(calendar_id=CALENDAR_ID, person=PERSON),
    ),
) -> Config:
    return Config(
        db_path="unused.sqlite3",  # type: ignore[arg-type]
        connector="fixture",
        classifier="stub",
        hub=HubConfig(base_url="http://127.0.0.1:8000", model="m"),
        notifier="telegram",
        telegram=TelegramConfig(bot_token="t", chat_id="c"),
        linked_device_dir="ld",  # type: ignore[arg-type]
        calendar=CalendarConfig(accounts=accounts),
        traffic=TrafficConfig(api_key="routes-key", origin_lookback_min=60),
        family=FamilyConfig(
            enabled=True,
            home_address=HOME,
            travel_blocks=TravelBlocksConfig(
                enabled=enabled, dry_run=dry_run, horizon_days=2, title_template=TITLE
            ),
        ),
    )


def sweep_horizon() -> tuple[datetime, datetime]:
    """The sweep's real planning window for :data:`NOW`.

    Computed, never hardcoded: it starts at the *machine's* local midnight, so a
    literal would only be right in one timezone.
    """
    return travel_blocks.travel_block_horizon(make_config(), NOW)


def run_sweep(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    events: dict[str, list[Any]] | None = None,
    read: FakeReadClient | None = None,
    write: FakeWriteClient | None = None,
    write_client_error: Exception | None = None,
    route_fn: StubRoutes | None = None,
    now: datetime = NOW,
    **config_kwargs: Any,
) -> tuple[dict[str, Any], FakeReadClient, FakeWriteClient]:
    """Drive `run_travel_blocks` end to end with both clients faked."""
    from calendar_readonly.core import normalize_event

    read_client = read or FakeReadClient()
    write_client = write or FakeWriteClient()
    write_client.backup_root = tmp_path

    def build_write(_path: Path) -> FakeWriteClient:
        if write_client_error is not None:
            raise write_client_error
        return write_client

    monkeypatch.setattr(calendar_source, "build_google_calendar_client", lambda _p: read_client)
    monkeypatch.setattr(travel_blocks_write, "build_google_calendar_write_client", build_write)

    raw = events if events is not None else {PERSON: [make_raw_event("e1")]}
    normalized = {
        person: [normalize_event(item, calendar_id=CALENDAR_ID) for item in items]
        for person, items in raw.items()
    }
    payload = travel_blocks_write.run_travel_blocks(
        make_config(**config_kwargs),
        normalized,
        now=now,
        route_fn=route_fn or StubRoutes(),
        backup_root=tmp_path,
    )
    return payload, read_client, write_client


def stub_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire `run_calendar_scan` for offline use: fake read client, no events, no alert."""
    from src.family import calendar_scan

    monkeypatch.setattr(
        calendar_source, "build_google_calendar_client", lambda _p: FakeReadClient()
    )
    monkeypatch.setattr(calendar_scan, "fetch_events_by_person", lambda *_a, **_k: {PERSON: []})
    monkeypatch.setattr(calendar_scan, "send_alert", lambda *_a, **_k: ("sent", None))
