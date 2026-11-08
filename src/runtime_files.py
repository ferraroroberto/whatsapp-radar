"""Writers for runtime files whose path the runtime-write ratchet cannot resolve.

State lives where a parameter, a config value or an environment override says
(``webapp/``, ``config/``, ``data/``, a linked-device buffer folder, a database a
caller opens). The ratchet cannot follow such a path statically, so every write
would need its own exempt marker. These writers take the path, refuse it at
runtime when it resolves under one of the code folders the ratchet guards, and
then write. The guarantee a marker only claimed is enforced here.

Gitignored live state inside the checkout (``config/``, ``webapp/``, ``data/``)
stays allowed so the paths a running tray already uses keep working.

Stdlib-only and import-free of the rest of the repo, so any module can use it.
"""

from __future__ import annotations

import logging
import os
import shutil
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import IO, Any, Union

PathLike = Union[str, "os.PathLike[str]"]

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The folders `[runtime_write_ratchet] code_folders` guards in `.fleet.toml`.
CODE_FOLDERS = ("src", "app", "scripts")


def checked(path: PathLike) -> Path:
    """``path`` resolved, or ``ValueError`` when it sits under a code folder."""
    resolved = Path(path).expanduser().resolve()
    for name in CODE_FOLDERS:
        folder = REPO_ROOT / name
        if resolved == folder or folder in resolved.parents:
            raise ValueError(
                f"Refusing to write under the code folder {folder}: {resolved}. "
                "Runtime output belongs in webapp/, config/, data/ or the runtime data root."
            )
    return resolved


def make_dirs(path: PathLike, *, parents: bool = True, exist_ok: bool = True) -> Path:
    """Create the folder ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named folder; checked() raises under a code folder
    target.mkdir(parents=parents, exist_ok=exist_ok)
    return target


def open_for_write(path: PathLike, mode: str = "w", **kwargs: Any) -> IO[Any]:
    """Open ``path`` with ``mode`` (creating no folders); refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named file; checked() raises under a code folder
    return target.open(mode, **kwargs)


def save_text(path: PathLike, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` in one call; refuses a path under a code folder."""
    with open_for_write(path, "w", encoding=encoding) as handle:
        handle.write(text)


def copy_file(src: PathLike, dst: PathLike) -> Path:
    """Copy ``src`` to ``dst`` keeping its metadata; refuses a destination under a code folder."""
    target = checked(dst)
    # runtime-write-exempt: caller-named destination; checked() raises under a code folder
    shutil.copy2(src, target)
    return target


def file_handler(path: PathLike, encoding: str = "utf-8") -> logging.FileHandler:
    """A ``logging.FileHandler`` on ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named log file; checked() raises under a code folder
    return logging.FileHandler(target, encoding=encoding)


def rotating_file_handler(path: PathLike, **kwargs: Any) -> RotatingFileHandler:
    """A ``RotatingFileHandler`` on ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named log file; checked() raises under a code folder
    return RotatingFileHandler(target, **kwargs)
