"""A slow read served from memory, refreshed off the request path while watched.

``get()`` returns the last built value with its age. A background thread
rebuilds it every ``refresh_s`` only while someone is still reading (within
``demand_s``), so an unwatched app polls nothing upstream. A value older than
``stale_s`` — or built for a different ``key`` — is rebuilt inline instead,
never served as current. A build that raises is never cached.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar

_log = logging.getLogger(__name__)

T = TypeVar("T")


class DemandSnapshot(Generic[T]):
    def __init__(
        self,
        build: Callable[[], T],
        key: Callable[[], str],
        *,
        refresh_s: float,
        demand_s: float,
        stale_s: float,
        name: str = "snapshot",
    ) -> None:
        self._build = build
        self._key = key
        self._refresh_s = refresh_s
        self._demand_s = demand_s
        self._stale_s = stale_s
        self._name = name
        self._lock = threading.Lock()
        self._state = threading.Condition()
        self._value: T | None = None
        self._built_for = ""
        self._built_at = 0.0
        self._last_read = 0.0
        self._ticker: threading.Thread | None = None
        self._closed = False

    def get(self) -> tuple[T, float]:
        """The current value and its age in seconds."""
        key = self._key()
        self._touch()
        hit = self._fresh(key)
        if hit is not None:
            return hit
        with self._lock:
            # A concurrent reader may have rebuilt while this one waited.
            hit = self._fresh(key)
            if hit is not None:
                return hit
            return self._rebuild(key), 0.0

    def close(self) -> None:
        with self._state:
            self._closed = True
            self._state.notify_all()

    def _fresh(self, key: str) -> tuple[T, float] | None:
        value, built_for, built_at = self._value, self._built_for, self._built_at
        age = time.monotonic() - built_at
        if value is None or built_for != key or age > self._stale_s:
            return None
        return value, age

    def _rebuild(self, key: str) -> T:
        value = self._build()
        self._value, self._built_for, self._built_at = value, key, time.monotonic()
        return value

    def _touch(self) -> None:
        with self._state:
            self._last_read = time.monotonic()
            if self._closed or (self._ticker is not None and self._ticker.is_alive()):
                return
            self._ticker = threading.Thread(
                target=self._tick, name=f"{self._name}-refresh", daemon=True
            )
            self._ticker.start()

    def _tick(self) -> None:
        while True:
            with self._state:
                if self._closed:
                    return
                self._state.wait(timeout=self._refresh_s)
                if self._closed or time.monotonic() - self._last_read > self._demand_s:
                    # Idle: end the thread; the next read starts a fresh one.
                    self._ticker = None
                    return
            try:
                with self._lock:
                    self._rebuild(self._key())
            except Exception as exc:
                _log.warning(f"⚠️ {self._name} background refresh failed: {exc}")
