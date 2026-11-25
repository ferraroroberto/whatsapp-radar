"""Thin launcher — the entrypoint meant to be invoked by humans / .bat files.

Usage:
    python scripts/launcher.py status                 # connector + DB status
    python scripts/launcher.py ingest                 # ingest chats/messages
    python scripts/launcher.py chats [--recent] [--limit N]
    python scripts/launcher.py monitor <chat_id>
    python scripts/launcher.py ignore <chat_id>
    python scripts/launcher.py review [--dry-run]
    python scripts/launcher.py scan [--dry-run] [--days N]
    python scripts/launcher.py notify [--run N]
    python scripts/launcher.py resync                  # incremental upsert from the buffer
    python scripts/launcher.py reprocess --confirm     # full rebuild (preserves operator state)

The launcher puts the repo root (its parent folder) on ``sys.path`` so the top-level packages
(``src``, ``app``) resolve without any outer namespace — clone the repo,
``pip install -r requirements.txt``, run the launcher.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.cli.stack_watchdog import arm_for_argv  # noqa: E402

# Armed before the app's own imports so a stall inside them is dumped too.
if __name__ == "__main__":
    arm_for_argv(sys.argv)

from app.cli.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
