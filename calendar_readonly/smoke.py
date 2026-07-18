"""Live read-only smoke check for the Calendar OAuth token (issue #160).

Non-interactive: given a minted token, probe each calendar and count upcoming
events over a short window, printing only privacy-safe aggregates (a masked
summary + event count + soonest date) — never full titles or attendee data.
This is the "does the refresh token actually read the calendars" validation
that runs after the interactive bootstrap.

    python -m calendar_readonly.smoke --calendar you@example.com --days 3
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from calendar_readonly.core import CalendarReadError, normalize_event
from calendar_readonly.google_client import build_google_calendar_client


def _mask(text: str) -> str:
    text = text.strip()
    if not text:
        return "(no title)"
    return f"{text[:3]}… ({len(text)} chars)"


def main(argv: list[str] | None = None) -> int:
    # Redirected/captured stdout (App Launcher's output.log, a pipe) falls back
    # to cp1252 on Windows, so the ✅/❌ status glyphs raise UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--token", type=Path, default=Path("auth/calendar/token.json"))
    parser.add_argument(
        "--calendar",
        action="append",
        required=True,
        help="calendar id (an email address); repeat for several",
    )
    parser.add_argument("--days", type=int, default=3)
    args = parser.parse_args(argv)

    now = datetime.now(UTC)
    window_end = now + timedelta(days=max(1, args.days))

    try:
        client = build_google_calendar_client(args.token)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"❌ {exc}")
        return 1

    exit_code = 0
    try:
        for calendar_id in args.calendar:
            try:
                summary = client.calendar_summary(calendar_id)
                raw_events = client.list_events(
                    calendar_id=calendar_id, time_min=now, time_max=window_end
                )
            except Exception as exc:  # noqa: BLE001 — report per-calendar, keep going
                print(f"❌ {calendar_id}: {CalendarReadError(str(exc))}")
                exit_code = 1
                continue
            events = [normalize_event(raw, calendar_id=calendar_id) for raw in raw_events]
            print(
                f"✅ {calendar_id} ({_mask(summary)}): "
                f"{len(events)} event(s) in the next {args.days}d"
            )
            for event in events[:3]:
                when = event.start.date().isoformat()
                print(f"     • {when}  {_mask(event.summary)}")
    finally:
        client.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
