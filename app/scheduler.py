"""Tiny background scheduler for the auto-sync safety net (no extra dependencies)."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from .logsafe import where

log = logging.getLogger("clearline.scheduler")


async def run_every(seconds: float, job: Callable[[], Awaitable[object]], *,
                    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
                    runs: int | None = None) -> None:
    """Call `job` every `seconds`, forever (or `runs` times, for tests). An error in one run
    is logged and never stops the loop; cancelling the task stops it cleanly."""
    done = 0
    while runs is None or done < runs:
        await sleep(seconds)
        try:
            await job()
        except Exception as err:
            log.error("Scheduled job failed (%s); will try again in %.0f s", where(err), seconds)
        done += 1
