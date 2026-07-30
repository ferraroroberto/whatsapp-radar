"""The repository-root anchor, shared by every surface.

``PROJECT_ROOT`` used to live in :mod:`app.webapp.routers._helpers`, which
imports FastAPI — so anything that wanted a repo-relative path pulled the web
framework in with it. That was harmless while only the webapp needed one, and a
problem the moment the CLI had to reuse the webapp's run-record helpers (#233):
every ``wr.bat status`` would have imported FastAPI just to learn where the repo
is. Keeping the anchor in a dependency-free module lets ``src`` and both ``app``
surfaces share one definition instead of each re-deriving it.
"""

from __future__ import annotations

from pathlib import Path

#: Repository root — the directory holding ``launcher.py``.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
