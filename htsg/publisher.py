"""Realtime delivery of complete publish groups (终版 §6, §11, §13.3).

The owner publishes one group per consistent state change.  This module packs those groups, in order and
whole, into messages that leave at most `push_coalesce` (100 ms) after the first of them arrived - several
groups share a message but none is dropped or merged into a final state.  A client that joins gets the
owner's whole state at version N first and every group after N next, so nothing is missed or applied twice.
When nothing was sent for `push_heartbeat` (1 s) a heartbeat leaves; it carries no sample and no channel.

Everything is synchronous except `run()`, which only decides WHEN `pump()` is called; the clocks are passed
in, so tests drive time exactly.  A message is shared by all clients and must not be changed once sent.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict

from . import records as rec


def encode_group(group: rec.PublishGroup, changed: dict) -> dict:
    """One group on the wire: the channels whose state it changed (inputs and the results computed from
    them together), the lifecycle records and the confirmed faults."""
    return {
        "seq": group.pub_seq,
        "order": group.order,
        "source": group.source_id,
        "wall": group.wall,
        "channels": changed,
        "states": [
            {"kind": s.kind, "source": s.source_id, "seg": s.seg, "wall": s.at_wall, "detail": s.detail}
            for s in group.states
        ],
        "faults": [asdict(f) for f in group.faults],
    }


class Publisher:
    def __init__(self, owner, clock=None):
        timing = owner.config.timing
        self.owner = owner
        self.coalesce, self.heartbeat = timing.push_coalesce, timing.push_heartbeat
        self.wall, self.mono = clock or (time.time, time.monotonic)
        self.views = owner.channel_views()  # what the groups offered so far add up to
        self.last_seq = owner.pub_seq
        self.pending: list[dict] = []
        self.due: float | None = None  # when the oldest pending group must leave
        self.last_sent = self.mono()
        self.subscribers: list = []
        self.wake = asyncio.Event()

    def offer(self, group: rec.PublishGroup):
        """The owner's `publish` sink.  Runs inside the owner's call, so the views are exactly the state this
        group leaves behind."""
        views = self.owner.channel_views()
        changed = {pid: v for pid, v in views.items() if self.views.get(pid) != v}
        self.views, self.last_seq = views, group.pub_seq
        self.pending.append(encode_group(group, changed))
        if self.due is None:
            self.due = self.mono() + self.coalesce
            self.wake.set()

    def subscribe(self, deliver):
        """Register a client: `deliver(message)` gets the snapshot first, then every later message in order.

        What is still waiting to leave goes out to the existing clients first, so the snapshot is the state
        after the last group offered and every group after it is new to this client.
        """
        self._flush()
        snapshot = self.owner.snapshot()
        if snapshot["pub_seq"] != self.last_seq:
            raise RuntimeError("publisher is not fed by this owner")
        deliver({"type": "snapshot", "wall": self.wall(), **snapshot})
        self.subscribers.append(deliver)

    def unsubscribe(self, deliver):
        if deliver in self.subscribers:
            self.subscribers.remove(deliver)

    def pump(self):
        """Send what is due: the packed groups once the oldest has waited the coalescing time, otherwise a
        heartbeat when nothing has left for a heartbeat interval."""
        now = self.mono()
        if self.pending:
            if now >= self.due:
                self._flush()
        elif now - self.last_sent >= self.heartbeat:
            self._send({"type": "heartbeat", "seq": self.last_seq})

    def _flush(self):
        if not self.pending:
            return
        groups, self.pending, self.due = self.pending, [], None
        self._send({"type": "groups", "first": groups[0]["seq"], "last": groups[-1]["seq"], "groups": groups})

    def _send(self, message: dict):
        message["wall"], message["status"] = self.wall(), self.owner.status()
        self.last_sent = self.mono()
        for deliver in list(self.subscribers):
            deliver(message)

    async def run(self):
        """Call `pump()` when the oldest pending group is due or the heartbeat is."""
        while True:
            deadline = self.due if self.due is not None else self.last_sent + self.heartbeat
            self.wake.clear()
            try:
                await asyncio.wait_for(self.wake.wait(), max(0.0, deadline - self.mono()))
            except TimeoutError:
                pass
            self.pump()
