"""Bounded entry between protocol callbacks and the single owner (终版 §4: 入口丢失按失步处理)."""

from __future__ import annotations

from collections import deque

from .owner import Owner
from .records import InputBatch


class _Loss:
    """In-order marker: batches were refused here, after everything queued before it."""

    __slots__ = ("source_id", "conn_gen", "wall", "mono", "tick", "count")

    def __init__(self, batch: InputBatch):
        self.source_id, self.conn_gen = batch.source_id, batch.conn_gen
        self.wall, self.mono, self.tick, self.count = batch.wall, batch.mono, batch.tick, 1


class Ingress:
    def __init__(self, owner: Owner, capacity: int):
        self.owner, self.capacity = owner, capacity
        self.queue: deque[InputBatch | _Loss] = deque()
        self.pending = 0  # batches only; loss markers do not count against the capacity
        self.dropped = 0

    def offer(self, batch: InputBatch) -> bool:
        """Never blocks.  A full queue becomes an ordered loss report to the owner, not a log line."""
        if self.pending >= self.capacity:
            self.dropped += 1
            last = self.queue[-1] if self.queue else None
            if isinstance(last, _Loss) and (last.source_id, last.conn_gen) == (batch.source_id, batch.conn_gen):
                last.count, last.tick = last.count + 1, batch.tick
            else:
                self.queue.append(_Loss(batch))
            return False
        self.queue.append(batch)
        self.pending += 1
        return True

    def drain(self, limit: int | None = None) -> int:
        n = 0
        while self.queue and (limit is None or n < limit):
            item = self.queue.popleft()
            if isinstance(item, _Loss):
                self.owner.report_loss(
                    item.source_id, item.conn_gen, "INGRESS_FULL", item.wall, item.mono, tick=item.tick, count=item.count
                )
            else:
                self.pending -= 1
                self.owner.accept(item)
            n += 1
        return n
