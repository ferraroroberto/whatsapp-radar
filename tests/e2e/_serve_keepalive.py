"""Launch the disposable e2e webapp with a longer idle keep-alive timeout.

The app polls every 15 s while hypercorn closes an idle connection after 5 s, so a
browser that sits on a page for a few seconds opens a fresh connection for each
poll, and every one it or the server then closes leaves a loopback ephemeral port
in TIME_WAIT (see ``_port_pool.py``). Holding idle connections for a minute lets a
test's page keep using the connections it has. Only the harness's server is
launched this way; the real app keeps the vendored ``serve.py`` defaults.

Run as ``python -m tests.e2e._serve_keepalive <the arguments app.webapp.serve takes>``.
"""

from __future__ import annotations

import sys
from typing import Any

from app.webapp import serve

HARNESS_KEEP_ALIVE_S = 60.0


def build_config(*args: Any, **kwargs: Any) -> Any:
    cfg = _vendored_build_config(*args, **kwargs)
    cfg.keep_alive_timeout = HARNESS_KEEP_ALIVE_S
    return cfg


_vendored_build_config = serve.build_config
serve.build_config = build_config

if __name__ == "__main__":
    sys.exit(serve.main())
