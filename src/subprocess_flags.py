"""Windows console-suppression flags for subprocess spawns.

A console subprocess launched from a windowless parent (a ``pythonw`` tray, or
any of its descendants — the webapp uvicorn process, a background
``launcher.py`` run, ...) gets a **new, visible** console window from Windows
unless the child's creation flags say otherwise. These constants are the one
place that combination lives, so a call site reuses it instead of re-deriving
it (and risking forgetting it — the exact gap that let stray cmd windows
through, see #207).

Safe to pass even when the parent *does* have a console, as long as the
child's output is captured (piped/redirected) rather than read from a console
directly. Both resolve to ``0`` — a pure no-op — on non-Windows.
"""

from __future__ import annotations

import subprocess

#: Suppress the console window for a one-shot, output-captured child.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: For a long-lived child that must not take the parent's console signals
#: (Ctrl+C / Ctrl+Break) with it, and can be stopped via a process-group signal
#: (CTRL_BREAK) rather than killed outright — e.g. cloudflared, the webapp's
#: uvicorn. Also the flag for a child meant to outlive this process (the Node
#: sidecar, a background ``launcher.py`` run): a Windows child survives its
#: parent's exit on its own. Never add ``DETACHED_PROCESS`` — it is mutually
#: exclusive with ``CREATE_NO_WINDOW``, which wins only by an undocumented
#: accident of precedence.
NO_WINDOW_NEW_GROUP = NO_WINDOW | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
