"""Bounded, ordered entry between protocol tasks and the single owner (终版 §4: 入口丢失按失步处理).

Batches and lifecycle events (new generation, disconnect, definite loss) share one queue, so the owner
sees them in the order they happened.  Only batches count against the capacity; lifecycle events are
small, rare and must never be refused.

Diagnostic facts for the communication log (§15.1) travel the same queue, so they keep their order, but they are
counted on their own and capped.  A full allowance only counts one more lost diagnostic: no APP_LOSS, no C1, nothing
taken from the batch capacity, no lifecycle event ever held up.
"""

from __future__ import annotations

from collections import deque

from .commlog import DIAG_LIMIT, clean_detail
from .owner import Owner
from .records import InputBatch


class _Event:
    __slots__ = ("method", "args", "count", "diag")

    def __init__(self, method: str, *args, diag: bool = False):
        self.method, self.args, self.count, self.diag = method, args, 1, diag


class Ingress:
    def __init__(self, owner: Owner, capacity: int, diag_capacity: int = DIAG_LIMIT):
        self.owner, self.capacity, self.diag_capacity = owner, capacity, diag_capacity
        self.queue: deque[InputBatch | _Event] = deque()
        self.pending = 0  # batches only
        self.dropped = 0
        self.diag_pending = 0  # diagnostic facts waiting; never mixed into `pending`
        self.diag_dropped = 0
        self._diag_told = 0  # how many of the dropped ones the owner has been told about
        self.wakeup = None  # optional callable invoked after anything is queued (runtime sets it)

    def _push(self, item):
        self.queue.append(item)
        if self.wakeup is not None:
            self.wakeup()

    # -- lifecycle events, in order with the batches -------------------------------------------------

    def begin_generation(self, source_id, gen, wall, mono, reason="CONNECT"):
        self._push(_Event("begin_generation", source_id, gen, wall, mono, reason))

    def begin_waiting(self, source_id, gen, mono):
        """The subscription of `gen` exists (CreateSubscription returned): the owner's 3 s / 6 s checks start here."""
        self._push(_Event("begin_waiting", source_id, gen, mono))

    def source_down(self, source_id, gen, reason, wall, mono):
        self._push(_Event("source_down", source_id, gen, reason, wall, mono))

    def lost(self, source_id, gen, reason, wall, mono):
        self._push(_Event("lost", source_id, gen, reason, wall, mono))

    def items_refused(self, source_id, gen, point_ids, wall, mono):
        """Monitored items the server refused while it took the others: in order with the generation's batches."""
        self._push(_Event("items_refused", source_id, gen, tuple(point_ids), wall, mono))

    # -- facts for the communication log -------------------------------------------------------------------------

    def attempt_failed(self, source_id, phase, reason, detail, wall, mono) -> bool:
        """A CONNECT / SUBSCRIBE attempt failed.  `detail` is cleaned and cut HERE, so the queue stays small whatever the
        library's message looked like.  False when the diagnostic allowance is full (counted, not an application loss)."""
        if self.diag_pending >= self.diag_capacity:
            self.diag_dropped += 1
            return False
        text, truncated = clean_detail(detail)
        self.diag_pending += 1
        self._push(_Event("attempt_failed", source_id, phase, reason, text, truncated, wall, mono, diag=True))
        return True

    def attempt_ok(self, source_id, phase, wall, mono):
        """The phase succeeded.  A boundary of the log's aggregates, so like a lifecycle event it is never refused."""
        self._push(_Event("attempt_ok", source_id, phase, wall, mono))

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
        if self.diag_dropped != self._diag_told:  # a count, not an event in the order of things: the owner only needs the total
            self.owner.report_diag_lost(self.diag_dropped - self._diag_told)
            self._diag_told = self.diag_dropped
        n = 0
        while self.queue and (limit is None or n < limit):
            item = self.queue.popleft()
            if isinstance(item, _Event):
                if item.diag:
                    self.diag_pending -= 1
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
