"""Fixed one-second acquisition cadence; missed ticks are never replayed in a burst."""

import asyncio
import time


async def every_second(*, immediate=True):
    deadline = time.monotonic() + (0 if immediate else 1)
    while True:
        await asyncio.sleep(max(0, deadline - time.monotonic()))
        yield
        deadline += 1
        now = time.monotonic()
        if deadline <= now:
            deadline = now + 1
