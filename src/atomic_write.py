"""Atomic text writes: a reader sees the old file or the new one, never half of either.

The one place this repo calls ``os.replace`` for a write. Each write goes to a
unique ``mkstemp`` sibling of the target (two writers of one file never share a
temp), then ``os.replace`` swaps it in. When the write or the swap raises, the
temp is unlinked and the error re-raised, so the old content stays intact, no
orphan is left beside it, and each caller keeps its own failure policy. The
parent folder must already exist.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` as UTF-8."""
    fd, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


__all__ = ["atomic_write_text"]
