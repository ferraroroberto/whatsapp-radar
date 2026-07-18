"""Live smoke check for the Google Routes API key (issue #160).

    .\\.venv\\Scripts\\python.exe -m scripts.traffic_smoke --origin "..." --dest "..."

Reads the key from ``--api-key``, else the ``traffic.api_key`` in the ignored
``config/local.json``, else the ``GOOGLE_MAPS_API_KEY`` environment variable.
Prints one line: free-flow vs. traffic minutes, the delay, and the status.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from src.traffic import TrafficReadError, compute_route


def _resolve_api_key(explicit: str) -> str:
    if explicit:
        return explicit
    local = Path("config/local.json")
    if local.is_file():
        try:
            raw = json.loads(local.read_text(encoding="utf-8"))
            key = str((raw.get("traffic") or {}).get("api_key") or "")
            if key:
                return key
        except (OSError, json.JSONDecodeError):
            pass
    return os.environ.get("GOOGLE_MAPS_API_KEY", "")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(description="Routes API v2 live smoke check")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--dest", required=True)
    parser.add_argument("--api-key", default="")
    args = parser.parse_args(argv)
    try:
        result = compute_route(args.origin, args.dest, api_key=_resolve_api_key(args.api_key))
    except TrafficReadError as exc:
        print(f"❌ {exc}")
        return 1
    print(
        f"✅ normal {result.normal_s // 60}m · traffic {result.traffic_s // 60}m · "
        f"delay {result.delay_min}m · {result.status}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
