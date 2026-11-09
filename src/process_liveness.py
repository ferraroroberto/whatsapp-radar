"""Whether a process id belongs to a live process, on Windows and POSIX.

A leaf module: the sidecar launcher and the run store both ask this question and
neither may import the other. :func:`pid_state` is three-valued because the two
callers need different things from a failed probe. The run store acts only on a
proven-dead pid (a probe it could not complete must leave its row alone), while
the sidecar launcher treats anything it cannot confirm as not running.
"""

from __future__ import annotations

import os
import sys

#: Windows ``OpenProcess`` error for a pid that names no process.
_ERROR_INVALID_PARAMETER = 87
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _windows_pid_state(pid: int) -> bool | None:
    import ctypes  # local import: Windows-only

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # Only "no such process" proves the pid is gone; access denied and the
        # rest say nothing about whether it is running.
        if ctypes.get_last_error() == _ERROR_INVALID_PARAMETER:
            return False
        return None
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return None
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def pid_state(pid: int) -> bool | None:
    """``True`` live, ``False`` proven gone, ``None`` when the probe cannot tell.

    Never raises. A non-positive pid names no process anyone could own, so it is
    ``None`` rather than ``False``: it is a malformed record, not a dead process.
    """
    if pid <= 0:
        return None
    try:
        if sys.platform == "win32":
            return _windows_pid_state(pid)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but belongs to someone else.
        return True
    except (OSError, ValueError, AttributeError):
        return None


def process_alive(pid: int) -> bool:
    """Best-effort liveness: ``True`` only for a pid confirmed live."""
    return pid_state(pid) is True
