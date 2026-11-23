"""The webapp's named worker pools: one per class of blocking work a route hands off.

The event loop's shared default pool would let a slow hub call or an ffmpeg
transcode hold the threads the 1.5 s run poll needs, so each class of work gets
its own bounded :class:`TimedExecutor`. A pool's queue and worker time show up in
the request's ``Server-Timing`` header, named after the pool.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, TypeVar

from src.request_timing import TimedExecutor

T = TypeVar("T")

# The Activity run poll: a short read of the runs folder, never queued behind slow work.
HOT_POLL = TimedExecutor("hot-poll", 2)
# A tap that waits on something outside this process (the hub, task-os, ffmpeg).
ON_DEMAND = TimedExecutor("on-demand", 4)


async def run_in(pool: TimedExecutor, fn: Callable[..., T], *args: Any) -> T:
    """Run ``fn(*args)`` on ``pool`` and await its result from the event loop."""
    return await asyncio.get_running_loop().run_in_executor(pool, fn, *args)
