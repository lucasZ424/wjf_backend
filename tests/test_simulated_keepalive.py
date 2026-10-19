"""R06/V04: project-owned asyncua servers emit keep-alives at the value they negotiate, without global patches.

What is measured is the time between CONSECUTIVE messages of a real subscription - a real asyncua server and client over the
loopback - within the contract's +-0.2 s.  A result outside it is a question of scheduling or implementation to look into, not a
reason to widen the tolerance.  The unadapted instances are the control that proves the correction is local: started after the
adapted ones they keep asyncua 2.0.1's count+2, and each is watched for 16 s.  Data changes are not held back for the keep-alive
count: a change goes out with the next publishing cycle.  The production validity stays 3 s (终版 §11).
"""

import asyncio
import time

import pytest
from owner_support import Recorder
from protocol_support import FieldUa
from test_source_validity import MONO0, WALL0, data, keepalive, tick, ua_only

from htsg.config import Point, Timing
from htsg.owner import Owner
from htsg.ua import ManagedClient, open_subscription

TOLERANCE = 0.2  # seconds, between consecutive messages (细则 R06)


class Watch:
    """A real subscription to a FieldUa server: when each message arrived, and the values the data ones carried."""

    def __init__(self, count, *, adapted=True):
        self.count, self.adapted = count, adapted
        self.messages, self.changes = [], []  # (arrival, "data" | "keepalive"); (arrival, value)
        self.field, self.client, self.sub, self.negotiated = None, None, None, None

    async def __aenter__(self):
        self.field = FieldUa(["a"], keepalive_adapter=self.adapted)
        await self.field.start()
        try:
            self.client = ManagedClient(self.field.endpoint, timeout=2)
            await self.client.connect()
            point = Point(id="a", source="ua", unit="u", node_id=self.field.node_id("a"))
            self.sub, self.negotiated = await open_subscription(
                self.client, [point], Timing(ua_keepalive_count=self.count), self.receive
            )
        except BaseException:
            await self.__aexit__()
            raise
        return self

    async def __aexit__(self, *exc):
        if self.sub is not None:
            await self.sub.delete_quietly()
        if self.client is not None:
            await self.client.disconnect()
        await self.field.stop()

    def receive(self, result):
        now = time.monotonic()
        notifications = result.NotificationMessage.NotificationData
        if not notifications:
            self.messages.append((now, "keepalive"))
            return
        self.messages.append((now, "data"))
        for notification in notifications:
            for item in notification.MonitoredItems:
                self.changes.append((now, item.Value.Value.Value))

    async def until(self, condition, seconds, why):
        deadline = time.monotonic() + seconds
        while not condition():
            assert time.monotonic() < deadline, f"timed out: {why}"
            await asyncio.sleep(0.02)

    async def change(self, value):
        """Write a new value; returns the time from the write to the arrival of the notification that carries it."""
        seen, wrote = len(self.changes), time.monotonic()
        await self.field.set("a", value)
        await self.until(lambda: len(self.changes) > seen, 3, f"the change to {value}")
        assert self.changes[-1][1] == value
        return self.changes[-1][0] - wrote


def keepalive_gaps(messages):
    """The time from the message before to each keep-alive.  Any publish restarts the idle count, so a keep-alive is counted
    from the last thing the server sent - data or keep-alive."""
    return [later[0] - earlier[0] for earlier, later in zip(messages, messages[1:], strict=False) if later[1] == "keepalive"]


@pytest.mark.parametrize(
    ("adapted", "count", "expected", "seconds"),
    [(True, 2, 2.0, 6), (True, 1, 1.0, 6), (False, 2, 4.0, 16), (False, 1, 3.0, 16)],
    ids=["adapted-count-2", "adapted-count-1", "raw-count-2", "raw-count-1"],
)
async def test_idle_keepalives_come_at_the_interval_of_the_adapter_scope_every_time(adapted, count, expected, seconds):
    """The adapted instance follows MaxKeepAliveCount; a raw instance started afterwards retains asyncua 2.0.1's count+2
    behaviour, proving the correction did not patch the global class.  Every keep-alive of the watch is measured, not the
    first one only."""
    async with Watch(count, adapted=adapted) as watch:
        assert watch.negotiated.keepalive_count == count
        await watch.until(lambda: bool(watch.changes), 3, "the first data")
        await asyncio.sleep(seconds)
    gaps = keepalive_gaps(watch.messages)
    assert len(gaps) >= seconds // expected - 1, f"{len(gaps)} keep-alives in {seconds} s"
    assert gaps == pytest.approx([expected] * len(gaps), abs=TOLERANCE), gaps


async def test_a_change_goes_out_with_the_next_publishing_cycle_whatever_the_phase_and_is_not_held_for_the_keepalive_count():
    """"Data changes are not delayed": one publishing interval at most - a change held back until the keep-alive count (2) ran
    out would show as up to two.  Each write follows the previous arrival - which sits on a cycle boundary - by another time
    that is not a whole number of cycles, so the writes meet different phases of the publishing cycle."""
    delays = []
    async with Watch(2) as watch:
        await watch.until(lambda: bool(watch.changes), 3, "the first data")
        for n, pause in enumerate((1.1, 1.35, 1.6, 1.85, 1.25, 1.7)):
            await asyncio.sleep(pause)
            delays.append(await watch.change(100.0 + n))
    assert all(delay <= 1.0 + TOLERANCE for delay in delays), delays
    assert max(delays) - min(delays) > 0.5, f"the writes should meet different phases of the cycle: {delays}"


async def test_sparse_changes_are_delivered_promptly_and_the_keepalives_between_them_keep_their_cadence():
    """A change now and then among idle keep-alives: each change arrives within one publishing interval, and every keep-alive
    - also the one after a change, which restarts the idle count - comes count (2) intervals after the message before it."""
    delays = []
    async with Watch(2) as watch:
        await watch.until(lambda: bool(watch.changes), 3, "the first data")
        for n, quiet in enumerate((3.3, 3.9, 3.1)):
            await asyncio.sleep(quiet)  # keep-alives only, then a change at whatever phase that falls on
            delays.append(await watch.change(100.0 + n))
        await asyncio.sleep(3.0)
    assert all(delay <= 1.0 + TOLERANCE for delay in delays), delays
    gaps = keepalive_gaps(watch.messages)
    assert len(gaps) >= 4 and gaps == pytest.approx([2.0] * len(gaps), abs=TOLERANCE), gaps  # one in each quiet stretch


def stays_valid(period, phase):
    """Whether the source is VALID after every event, with a keep-alive every `period` seconds and the owner's one-second tick
    at `phase` seconds past the second."""
    owner = Owner(ua_only(), WALL0, MONO0, commit=Recorder().commit, run_id="phase")
    owner.begin_generation("ua", 1, WALL0, MONO0)
    data(owner, 101.0)
    events = [(102.0 + phase + n, "tick") for n in range(12)] + [
        (103.0 + period * n, "keepalive") for n in range(int(11 / period) + 1)
    ]
    for mono, kind in sorted(events, key=lambda event: (event[0], event[1] != "tick")):
        (tick if kind == "tick" else keepalive)(owner, mono)
        if owner.sources["ua"].state != "VALID":
            return False
    return True


@pytest.mark.parametrize("phase", [x / 10 for x in range(10)])
def test_two_second_keepalives_never_expire_at_any_one_second_owner_tick_phase(phase):
    """R06: with the corrected count-2 interval below the 3 s validity, scheduling phase cannot create a false expiry."""
    assert stays_valid(2.0, phase)


def test_three_second_keepalives_do_expire_at_some_owner_tick_phase():
    """The counter-example that gives the test above its meaning: with a keep-alive every 3 s - the validity itself - there IS
    a phase at which the tick finds the source expired.  Were there none, the 2 s test would pass for any interval."""
    failing = [phase for phase in (x / 10 for x in range(10)) if not stays_valid(3.0, phase)]
    assert failing, "3 s keep-alives expired the source at no phase: the phase test cannot tell 2 s from 3 s"
