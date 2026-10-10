"""Fixed one-second acquisition cadence; missed ticks are never replayed in a burst.

Each yield is the planned tick number.  When the loop is late the skipped periods are counted
into the number, so two consecutive numbers prove two consecutive planned rounds and a gap in
the numbers proves skipped rounds.  Only the monotonic clock is read: UTC steps cannot move it.
"""

import asyncio
import time


async def every_second(*, immediate=True):
    deadline = time.monotonic() + (0 if immediate else 1)
    number = 0 if immediate else 1
    while True:
        await asyncio.sleep(max(0, deadline - time.monotonic()))
        yield number
        number += 1
        deadline += 1
        now = time.monotonic()
        if deadline <= now:
            number += int(now - deadline) + 1  # whole planned periods that passed with no tick
            deadline = now + 1
