"""Run the webapp in the foreground with the settings the tray would use.

``scripts/webapp.bat`` calls this so a headless box and the tray read one source for host,
port, server and TLS (``webapp_config.json``, ``WR_WEBAPP_PORT``) instead of the
bat carrying its own copy of them.
"""

from __future__ import annotations

import logging
import sys

from app.webapp.manager import WebappManager, webapp_manager_config


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    return WebappManager(webapp_manager_config()).serve_foreground()


if __name__ == "__main__":
    sys.exit(main())
