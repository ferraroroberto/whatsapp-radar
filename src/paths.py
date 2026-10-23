"""The repository-root anchor, shared by every surface.

``PROJECT_ROOT`` lives here rather than in :mod:`app.webapp.routers._helpers`,
which imports FastAPI: the CLI reuses the webapp's run-record helpers, and
importing FastAPI on every ``wr.bat status`` just to learn where the repo is
would be wasteful. Keeping the anchor in a dependency-free module lets ``src``
and both ``app`` surfaces share one definition instead of each re-deriving it.
"""

from __future__ import annotations

from pathlib import Path

#: Repository root — the directory holding ``launcher.py``.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
