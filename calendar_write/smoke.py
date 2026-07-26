"""Live write smoke check for the Calendar write OAuth token (#217).

Non-interactive: given a minted write-scope token, insert one throwaway test
event a few minutes out on the target calendar, confirm it round-trips with an
id, then delete it again — proving the write token can create and remove
events and leaves nothing behind. Prints only privacy-safe aggregates (a
truncated event id), never full event content.

    python -m calendar_write.smoke --calendar you@example.com
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from calendar_write.core import CalendarWriteError
from calendar_write.google_client import build_google_calendar_write_client


def main(argv: list[str] | None = None) -> int:
    # Redirected/captured stdout (App Launcher's output.log, a pipe) falls back
    # to cp1252 on Windows, so the ✅/❌ status glyphs raise UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--token", type=Path, default=Path("auth/calendar/write_token.json")
    )
    parser.add_argument("--calendar", required=True, help="calendar id (an email address)")
    args = parser.parse_args(argv)

    start = datetime.now(UTC) + timedelta(minutes=5)
    end = start + timedelta(minutes=1)
    event = {
        "summary": "whatsapp-radar write-scope smoke test (safe to ignore/delete)",
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }

    try:
        client = build_google_calendar_write_client(args.token)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"❌ {exc}")
        return 1

    try:
        try:
            created = client.insert_event(calendar_id=args.calendar, event=event)
        except Exception as exc:  # noqa: BLE001 — report a privacy-safe error, keep going
            print(f"❌ insert failed: {CalendarWriteError(str(exc))}")
            return 1
        event_id = str(created.get("id") or "")
        print(f"✅ inserted test event {event_id[:8]}… on {args.calendar}")
        try:
            client.delete_event(calendar_id=args.calendar, event_id=event_id)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ cleanup delete failed — remove {event_id[:8]}… by hand: {exc}")
            return 1
        print(f"✅ deleted test event {event_id[:8]}… — write+delete round-trip confirmed")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
