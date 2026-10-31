"""Thin launcher — the entrypoint meant to be invoked by humans / .bat files.

Usage:
    python launcher.py status                 # connector + DB status
    python launcher.py ingest                 # ingest chats/messages
    python launcher.py chats [--recent] [--limit N]
    python launcher.py monitor <chat_id>
    python launcher.py ignore <chat_id>
    python launcher.py review [--dry-run]
    python launcher.py scan [--dry-run] [--days N]
    python launcher.py notify [--run N]
    python launcher.py resync                  # incremental upsert from the buffer
    python launcher.py reprocess --confirm     # full rebuild (preserves operator state)

The launcher puts its own folder on ``sys.path`` so the top-level packages
(``src``, ``app``) resolve without any outer namespace — clone the repo,
``pip install -r requirements.txt``, run the launcher.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from app.cli.stack_watchdog import arm_for_argv  # noqa: E402

# Armed before the app's own imports so a stall inside them is dumped too.
if __name__ == "__main__":
    arm_for_argv(sys.argv)

from app.cli.main import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
