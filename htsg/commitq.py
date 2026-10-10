"""Ordered, bounded queue between the single owner and the one database writer (终版 §5, §16.2-1).

One FIFO carries data groups, state groups and watermark groups, so a watermark can never overtake the
data it claims to cover.  The owner never waits for it: `offer` is O(1) and says no when full instead of
overwriting.  Database work runs on its own thread; only a CONFIRMED commit is announced.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from . import records as rec


class CommitQueue:
    def __init__(
        self, router, capacity: int, *, on_committed=None, on_refused=None, is_void=None, retry_delay=(0.05, 2.0)
    ):
        self.router, self.capacity = router, capacity
        self.on_committed, self.on_refused, self.is_void = on_committed, on_refused, is_void
        self.queue: deque[rec.CommitGroup] = deque()
        self.retry_delay = retry_delay
        self.wake = asyncio.Event()
        self.io = ThreadPoolExecutor(max_workers=1, thread_name_prefix="htsg-writer")
        self.stats = {
            "offered": 0,
            "rejected": 0,
            "committed": 0,
            "refused": 0,
            "voided": 0,
            "failures": 0,
            "unknown": 0,
            "last_commit_wall": None,
            "last_error": None,
            "highest_committed_order": 0,
        }
        self._stop = False

    def offer(self, group: rec.CommitGroup) -> bool:
        """False when full: the caller must treat that as archive loss, never as an accepted group."""
        if len(self.queue) >= self.capacity:
            self.stats["rejected"] += 1
            return False
        self.queue.append(group)
        self.stats["offered"] += 1
        self.wake.set()
        return True

    @property
    def depth(self) -> int:
        return len(self.queue)

    async def run(self):
        loop = asyncio.get_running_loop()
        delay = self.retry_delay[0]
        while not (self._stop and not self.queue):
            if not self.queue:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), 0.5)
                except TimeoutError:
                    pass
                continue
            group = self.queue[0]  # stays at the head until it is settled: nothing can overtake it
            if self.is_void is not None and self.is_void(group):
                self.queue.popleft()  # it sits behind a lost group; only the recovery unit may restore continuity
                self.stats["voided"] += 1
                continue
            result = await loop.run_in_executor(self.io, self.router.write_group, group)
            if result.durable:
                self.queue.popleft()
                delay = self.retry_delay[0]
                self.stats["committed"] += 1
                self.stats["last_commit_wall"] = time.time()
                self.stats["highest_committed_order"] = max(self.stats["highest_committed_order"], group.order)
                if self.on_committed:
                    self.on_committed(group, result)  # announced only now: the data is really durable
            elif result.status == "REFUSED":
                self.queue.popleft()
                delay = self.retry_delay[0]
                self.stats["refused"] += 1
                self.stats["last_error"] = result.reason
                if self.on_refused:
                    self.on_refused(group, result.reason)
            else:  # FAILED (certain) or UNKNOWN: the same group is tried again, same ids, same target
                self.stats["unknown" if result.status == "UNKNOWN" else "failures"] += 1
                self.stats["last_error"] = result.reason
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.retry_delay[1])

    async def stop(self, timeout: float = 8.0):
        """Let the writer settle what is queued, then end; whatever remains is reported, not hidden."""
        self._stop = True
        self.wake.set()
        deadline = time.monotonic() + timeout
        while self.queue and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.io.shutdown(wait=False, cancel_futures=True)
        return len(self.queue)
