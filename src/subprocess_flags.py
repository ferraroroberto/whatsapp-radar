"""Windows console-suppression flags for subprocess spawns.

A console subprocess launched from a windowless parent (a ``pythonw`` tray, or
any of its descendants — the webapp uvicorn process, a detached
``launcher.py`` run, ...) gets a **new, visible** console window from Windows
unless the child's creation flags say otherwise. These constants are the one
place that combination lives, so a call site reuses it instead of re-deriving
it (and risking forgetting it — the exact gap that let stray cmd windows
through, see #207).

Safe to pass even when the parent *does* have a console, as long as the
child's output is captured (piped/redirected) rather than read from a console
directly. All three resolve to ``0`` — a pure no-op — on non-Windows.
"""

from __future__ import annotations

import subprocess

#: Suppress the console window for a one-shot, output-captured child.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: For a long-lived child stopped via a process-group signal (CTRL_BREAK)
#: rather than killed outright — e.g. cloudflared, the webapp's uvicorn.
NO_WINDOW_NEW_GROUP = NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

#: For a child meant to fully detach and outlive this process — e.g. the
#: Node sidecar, a background ``launcher.py`` run.
NO_WINDOW_DETACHED = NO_WINDOW | getattr(subprocess, "DETACHED_PROCESS", 0)
