"""Bounded, ordered entry between protocol tasks and the single owner (终版 §4: 入口丢失按失步处理).

Batches and lifecycle events (new generation, disconnect, definite loss) share one queue, so the owner
sees them in the order they happened.  Only batches count against the capacity; lifecycle events are
small, rare and must never be refused.
"""

from __future__ import annotations

from collections import deque

from .owner import Owner
from .records import InputBatch


class _Event:
    __slots__ = ("method", "args", "count")

    def __init__(self, method: str, *args):
        self.method, self.args, self.count = method, args, 1


class Ingress:
    def __init__(self, owner: Owner, capacity: int):
        self.owner, self.capacity = owner, capacity
        self.queue: deque[InputBatch | _Event] = deque()
        self.pending = 0  # batches only
        self.dropped = 0
        self.wakeup = None  # optional callable invoked after anything is queued (runtime sets it)

    def _push(self, item):
        self.queue.append(item)
        if self.wakeup is not None:
            self.wakeup()

    # -- lifecycle events, in order with the batches -------------------------------------------------

    def begin_generation(self, source_id, gen, wall, mono, reason="CONNECT"):
        self._push(_Event("begin_generation", source_id, gen, wall, mono, reason))

    def source_down(self, source_id, gen, reason, wall, mono):
        self._push(_Event("source_down", source_id, gen, reason, wall, mono))

    def lost(self, source_id, gen, reason, wall, mono):
        self._push(_Event("lost", source_id, gen, reason, wall, mono))

    # -- batches --------------------------------------------------------------------------------------

    def offer(self, batch: InputBatch) -> bool:
        """Never blocks.  A full queue becomes an ordered loss report to the owner, not a log line."""
        if self.pending >= self.capacity:
            self.dropped += 1
            last = self.queue[-1] if self.queue else None
            if (
                isinstance(last, _Event)
                and last.method == "report_loss"
                and last.args[:2] == (batch.source_id, batch.conn_gen)
            ):
                last.count += 1
                last.args = (*last.args[:5], batch.tick)
            else:
                self._push(
                    _Event("report_loss", batch.source_id, batch.conn_gen, "INGRESS_FULL", batch.wall, batch.mono, batch.tick)
                )
            return False
        self.pending += 1
        self._push(batch)
        return True

    def drain(self, limit: int | None = None) -> int:
        n = 0
        while self.queue and (limit is None or n < limit):
            item = self.queue.popleft()
            if isinstance(item, _Event):
                if item.method == "report_loss":
                    source_id, gen, reason, wall, mono, tick = item.args
                    self.owner.report_loss(source_id, gen, reason, wall, mono, tick=tick, count=item.count)
                else:
                    getattr(self.owner, item.method)(*item.args)
            else:
                self.pending -= 1
                self.owner.accept(item)
            n += 1
        return n
