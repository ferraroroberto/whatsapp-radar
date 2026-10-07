"""Rotating file logging for the always-on processes (#351).

The tray runs under ``pythonw`` and spawns the webapp with stdout/stderr on
``DEVNULL``, and nothing in this repo configured a handler, so every
``logger.info`` breadcrumb was dropped and every warning or error from the
always-on process went nowhere. :func:`configure_file_logging` gives each entry
point (tray, webapp, CLI) its own rotating file under the ignored ``webapp/``
directory, so the next occurrence of a bug is diagnosable from logs.

Design choices, each deliberate:

- **One file per entry point** (``tray.log`` / ``webapp.log`` / ``cli.log``). They
  are separate processes, and Windows cannot rotate a file another process holds
  open, so sharing one file would make every rollover a race.
- **Only this repo's loggers** (the ``src`` and ``app`` namespaces) get the
  handler. Third-party libraries (HTTP clients, the Google API client) log request
  URLs at debug level and are left to Python's default, so they never reach the
  file.
- **No message content.** The repo's log lines carry ids, counts and exception
  text, never chat names or message bodies (``CLAUDE.md`` Hard Privacy Rules);
  keep it that way when adding log calls. The files are gitignored (``*.log``).
- ``WR_LOG_DIR`` overrides the directory; set it to an empty string to turn file
  logging off (the test suite does).
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from src.paths import PROJECT_ROOT

LOG_DIR_ENV = "WR_LOG_DIR"
DEFAULT_LOG_DIR = PROJECT_ROOT / "webapp"

_NAMESPACES = ("src", "app")
_MAX_BYTES = 1_000_000
_BACKUP_COUNT = 3
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def resolve_log_dir() -> Path | None:
    """The directory log files go in, or ``None`` when file logging is disabled."""
    raw = os.environ.get(LOG_DIR_ENV)
    if raw is None:
        return DEFAULT_LOG_DIR
    return Path(raw) if raw.strip() else None


def configure_file_logging(name: str, *, log_dir: Path | None = None) -> Path | None:
    """Attach the ``<name>.log`` rotating handler to this repo's loggers, once.

    Idempotent per ``(directory, name)``: ``create_app()`` and the CLI can both
    call it on every boot. Returns the log path, or ``None`` when logging is
    disabled or the file cannot be opened (a read-only checkout must never stop
    the app from starting).
    """
    target_dir = log_dir if log_dir is not None else resolve_log_dir()
    if target_dir is None:
        return None
    path = target_dir / f"{name}.log"

    anchor = logging.getLogger(_NAMESPACES[0])
    if any(
        isinstance(h, RotatingFileHandler) and Path(h.baseFilename).resolve() == path.resolve()
        for h in anchor.handlers
    ):
        return path

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8", delay=True
        )
    except OSError as exc:
        logging.getLogger(__name__).warning("⚠️ file logging unavailable (%s): %s", path, exc)
        return None

    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(_FORMAT))
    for namespace in _NAMESPACES:
        logger = logging.getLogger(namespace)
        logger.addHandler(handler)
        if logger.level == logging.NOTSET or logger.level > logging.INFO:
            logger.setLevel(logging.INFO)
    return path
