"""Live smoke check for the Google Routes API key.

    .\\.venv\\Scripts\\python.exe -m scripts.traffic_smoke --origin "..." --dest "..."

Reads the key from ``--api-key``, else the one the scheduled check uses
(``load_config().traffic.api_key``: ``WR_TRAFFIC_API_KEY``, then
``GOOGLE_MAPS_API_KEY``, then ``traffic.api_key`` in the config files), so the
smoke check exercises the same key as the runtime.
Prints one line: free-flow vs. traffic minutes, the delay, and the status.
"""

from __future__ import annotations

import argparse
import sys

from src.config import load_config
from src.traffic import TrafficReadError, compute_route


def _resolve_api_key(explicit: str) -> str:
    return explicit or load_config().traffic.api_key


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
