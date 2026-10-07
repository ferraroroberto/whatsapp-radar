"""Rotating file logging for the always-on processes (#351)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from src.logging_setup import LOG_DIR_ENV, configure_file_logging, resolve_log_dir


@pytest.fixture(autouse=True)
def _restore_loggers() -> Iterator[None]:
    """Undo handler/level changes so one test's file handler never leaks into the next."""
    names = ("src", "app", "urllib3")
    saved = {n: (list(logging.getLogger(n).handlers), logging.getLogger(n).level) for n in names}
    yield
    for name, (handlers, level) in saved.items():
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            if handler not in handlers:
                handler.close()
                logger.removeHandler(handler)
        logger.setLevel(level)


def _flush(logger_name: str) -> None:
    for handler in logging.getLogger(logger_name).handlers:
        handler.flush()


def test_repo_loggers_reach_the_file_but_third_party_loggers_do_not(tmp_path: Path) -> None:
    path = configure_file_logging("webapp", log_dir=tmp_path)

    assert path == tmp_path / "webapp.log"
    logging.getLogger("src.example").info("ℹ️ breadcrumb from src")
    logging.getLogger("app.example").warning("⚠️ warning from app")
    logging.getLogger("urllib3").info("third-party chatter")
    _flush("src")

    text = path.read_text(encoding="utf-8")
    assert "breadcrumb from src" in text
    assert "warning from app" in text
    assert "third-party chatter" not in text


def test_is_idempotent_so_repeat_boots_do_not_duplicate_lines(tmp_path: Path) -> None:
    configure_file_logging("cli", log_dir=tmp_path)
    configure_file_logging("cli", log_dir=tmp_path)

    logging.getLogger("src.example").info("only once")
    _flush("src")

    assert (tmp_path / "cli.log").read_text(encoding="utf-8").count("only once") == 1


def test_empty_log_dir_env_disables_file_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LOG_DIR_ENV, "")
    assert resolve_log_dir() is None
    assert configure_file_logging("tray") is None


def test_unwritable_location_degrades_to_no_file_logging(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")  # mkdir under a regular file raises OSError

    assert configure_file_logging("tray", log_dir=blocker / "logs") is None
