"""D44a: persistent re-synchronisation, subscription wait start and the source-alarm baseline (终版 §11 约束前三条).

Why it matters: "persistent resync" is how a source that keeps falling back into recovery is told apart from
one that is simply offline, so the count must follow real state transitions (never log lines or the backoff
counter) and must forget only after a proven stable spell.  A subscription that has not started waiting must not
be declared silent (it would be rebuilt in a loop before it ever existed), and a slow archive start-up must not
use up the time a source is allowed to stay quiet.

Dependence: 方案 D44a, 审核 §8.1 S1, §8.2 P2、E5、E8, §8.4/8.5 "实施时按此理解".
"""

import asyncio
import time

import pytest
from owner_support import Recorder, poll, small_config
from protocol_support import FieldUa, fast_ticker

from htsg import records as r
from htsg.config import Point, Settings, Timing
from htsg.ingress import Ingress
from htsg.modbus import ModbusAcquirer
from htsg.owner import Owner
from htsg.ua import ManagedSubscription, UaAcquirer

WALL0, MONO0 = 1000.0, 100.0
PROTOCOLS = ["modbus", "opcua"]


def at(mono):
    return WALL0 + (mono - MONO0), mono


class Bench:
    """One owner with ONLY the protocol under test enabled, and the words a protocol task would say to it."""

    def __init__(self, protocol, **timing):
        payload = small_config().model_dump(mode="json")
        self.sid = "mb" if protocol == "modbus" else "ua"
        for source in payload["sources"]:
            source["enabled"] = source["id"] == self.sid
        payload["timing"] = Timing(**timing).model_dump()
        self.rec = Recorder()
        self.owner = Owner(Settings.model_validate(payload), WALL0, MONO0, commit=self.rec.commit, run_id="run")
        self.protocol, self.rebuilds, self.round = protocol, [], 0
        self.owner.on_rebuild = lambda source, reason: self.rebuilds.append((source, reason))

    @property
    def s(self):
        return self.owner.sources[self.sid]

    @property
    def count(self):
        return self.s.resync_count

    def generation(self, gen, mono):
        """A connection (and for OPC UA also its subscription) exists: waiting for the first response starts."""
        self.owner.begin_generation(self.sid, gen, *at(mono))
        if self.protocol == "opcua":
            self.owner.begin_waiting(self.sid, gen, mono)

    def respond(self, gen, mono, how="answer"):
        """Valid communication: a Modbus round (answered, or the device's exception) / an OPC UA publish."""
        wall, mono = at(mono)
        if self.protocol == "modbus":
            self.round += 1
            if how == "exception":
                batch = poll(self.round, {}, outcome="EXCEPTION", source="mb", gen=gen, wall=wall, mono=mono)
            else:
                batch = poll(self.round, {"b1": 0.5, "b2": 0.5}, source="mb", gen=gen, wall=wall, mono=mono)
        elif how == "bad_value":  # a normal publish that carries a Bad value is still proof of communication
            item = r.Item("ti", None, r.BAD, 0x80000000)
            batch = r.InputBatch("ua", gen, r.KIND_UA_NOTIFY, wall, mono, (item,), publish_seq=self.round + 1)
        else:
            batch = r.InputBatch("ua", gen, r.KIND_UA_NOTIFY, wall, mono)  # keep-alive
        return self.owner.accept(batch)

    def timeout_round(self, gen, mono):
        self.round += 1
        wall, mono = at(mono)
        return self.owner.accept(poll(self.round, {}, outcome="TIMEOUT", source="mb", gen=gen, wall=wall, mono=mono))

    def down(self, gen, mono, reason="CONNECTION"):
        self.owner.source_down(self.sid, gen, reason, *at(mono))

    def tick(self, mono):
        self.owner.tick(*at(mono))

    def run(self, gen, start, stop, how="answer"):
        """One valid response and one tick per second, from `start` to `stop` inclusive."""
        for m in range(start, stop + 1):
            self.respond(gen, float(m), how)
            self.tick(float(m))

    def cycles(self, n, start=110):
        """`n` times: a new generation, then straight back into RESYNC (nothing answered)."""
        for k in range(1, n + 1):
            self.generation(k, start + 10 * k)
            self.down(k, start + 10 * k + 1)
        return start + 10 * n + 1


# --------------------------------------------------------------------------------- S1: counting


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_a_source_that_never_had_a_generation_counts_zero_and_is_not_declared_silent(protocol):
    b = Bench(protocol)
    for m in range(101, 400):
        b.tick(float(m))
    assert b.count == 0 and b.s.state == "WAITING" and b.rebuilds == [] and b.owner.health["resyncs"] == 0
    assert b.s.alarm, "the source-level alarm (10 s / 6 s without any response) runs on its own baseline"


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_one_disconnect_followed_by_endless_failed_reconnects_stays_at_one(protocol):
    b = Bench(protocol)
    b.generation(1, 101.0)
    b.respond(1, 102.0)
    b.down(1, 103.0)
    assert b.count == 1 and b.s.state == "RESYNC"
    b.down(1, 104.0)  # a repeated report of the same disconnect
    b.down(1, 105.0, "CONNECT_FAILED")  # a retry that fails while the source is already in RESYNC
    b.down(0, 106.0)  # an older generation reporting late
    assert b.respond(1, 107.0) is False, "the old generation's late message is dropped"
    for m in range(110, 400):
        b.tick(float(m))
    assert b.count == 1, "long offline is one resync; the long-offline fact is the alarm and the connect failures"


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_each_new_generation_that_falls_straight_back_into_resync_counts_and_the_fifth_is_persistent(protocol):
    b = Bench(protocol)
    for cycle in range(1, 6):
        b.generation(cycle, 100.0 + 10 * cycle)
        assert b.count == cycle - 1, "a new generation does not clear what was counted"
        b.down(cycle, 101.0 + 10 * cycle)
        assert b.count == cycle
        status = b.owner.status()["sources"][b.sid]
        assert status["resync_count"] == cycle and status["persistent_resync"] is (cycle >= 5)
    assert b.owner.health["resyncs"] == 5, "the same transitions, counted in total"


def test_an_opcua_silence_resync_and_a_lost_notification_each_count_one_transition():
    b = Bench("opcua")
    b.generation(1, 101.0)
    b.respond(1, 102.0)
    b.tick(108.0)  # 6 s without a response
    assert b.rebuilds == [("ua", "SILENCE")] and b.count == 1
    b.generation(2, 110.0)
    b.owner.lost("ua", 2, "SEQUENCE_GAP", *at(111.0))
    b.owner.lost("ua", 2, "OVERFLOW", *at(111.5))  # already in RESYNC
    assert b.count == 2


# ------------------------------------------------------------------ S1: the stable sixty seconds


@pytest.mark.parametrize(
    ("protocol", "how"),
    [("modbus", "answer"), ("modbus", "exception"), ("opcua", "answer"), ("opcua", "bad_value")],
)
def test_sixty_seconds_of_uninterrupted_valid_communication_clear_the_count(protocol, how):
    """Valid communication, not Good values: a keep-alive, a Bad value in a normal publish and a Modbus exception
    response all prove the link is alive."""
    b = Bench(protocol)
    end = b.cycles(4)
    b.generation(5, end + 5.0)
    first = int(end + 6)
    b.run(5, first, first + 59, how)
    assert b.count == 4, "59 s after the first response of this generation"
    b.run(5, first + 60, first + 60, how)
    assert b.count == 0 and b.owner.status()["sources"][b.sid]["persistent_resync"] is False


def test_a_timed_out_round_restarts_the_stable_period_although_the_state_stays_valid():
    b = Bench("modbus")
    end = b.cycles(3)
    b.generation(4, end + 5.0)
    first = int(end + 6)
    b.run(4, first, first + 29)
    b.timeout_round(4, float(first + 30))  # one ordinary timeout: the connection is kept, state stays VALID
    b.tick(float(first + 30))
    assert b.s.state == "VALID", "looking at the state alone would call this stable"
    b.run(4, first + 31, first + 90)
    assert b.count == 3, "the 60 s started again at the first answer after the timeout"
    b.run(4, first + 91, first + 91)
    assert b.count == 0


def test_an_expiry_inside_the_same_generation_restarts_the_stable_period():
    b = Bench("opcua")
    end = b.cycles(3)
    b.generation(4, end + 5.0)
    first = int(end + 6)
    b.run(4, first, first + 29)
    for m in range(first + 30, first + 34):  # 3 s without any response: EXPIRED, no rebuild
        b.tick(float(m))
    assert b.s.state == "EXPIRED" and b.count == 3
    b.run(4, first + 34, first + 93)  # the same subscription publishes again
    assert b.s.state == "VALID" and b.count == 3, "59 s after the recovery response"
    b.run(4, first + 94, first + 94)
    assert b.count == 0


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_a_new_generation_interrupts_the_stable_period(protocol):
    b = Bench(protocol)
    end = b.cycles(3)
    b.generation(4, end + 5.0)
    first = int(end + 6)
    b.run(4, first, first + 30)
    b.generation(5, first + 30.5)  # a rebuild started without a RESYNC in between
    b.run(5, first + 32, first + 91)
    assert b.count == 3, "counted from the first response of the NEW generation"
    b.run(5, first + 92, first + 92)
    assert b.count == 0


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_a_resync_interrupts_the_stable_period_and_the_transition_itself_is_counted(protocol):
    b = Bench(protocol)
    end = b.cycles(3)
    b.generation(4, end + 5.0)
    first = int(end + 6)
    b.run(4, first, first + 40)
    assert b.count == 3
    b.down(4, first + 41.0)
    assert b.count == 4
    b.generation(5, first + 45.0)
    b.run(5, first + 46, first + 105)
    assert b.count == 4
    b.run(5, first + 106, first + 106)
    assert b.count == 0


def test_the_alarm_also_interrupts_the_stable_period():
    """No answer for the whole alarm period: whatever answered before it cannot count towards stability."""
    b = Bench("modbus")
    end = b.cycles(2)
    b.generation(3, end + 5.0)
    first = int(end + 6)
    b.run(3, first, first + 20)
    for m in range(first + 21, first + 31):  # 10 s of silence
        b.tick(float(m))
    assert b.s.alarm
    b.run(3, first + 31, first + 90)
    assert b.count == 2
    b.run(3, first + 91, first + 91)
    assert b.count == 0


# ------------------------------------------------ S1 at the protocol tasks (not just at the owner)


class FakeModbusClient:
    """Accepts the connection, then never answers a request (a gateway whose device is off)."""

    def __init__(self, connects=True):
        self.connects = connects

    async def connect(self):
        return self.connects

    async def read_holding_registers(self, *args, **kwargs):
        from pymodbus.exceptions import ModbusIOException

        raise ModbusIOException("no response received")  # what pymodbus raises when the answer does not come

    read_input_registers = read_holding_registers

    def close(self):
        pass


async def drain_until(ingress, condition, timeout=20.0, why=""):
    """Hand the owner ONE queued item at a time and look after each, so a count is read the moment it is reached."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out: {why}"
        if not ingress.drain(1):
            await asyncio.sleep(0.002)


async def stop(*tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def modbus_bench(client_factory):
    payload = small_config().model_dump(mode="json")
    payload["sources"][1]["enabled"] = False
    config = Settings.model_validate(payload)
    owner = Owner(config, time.time(), time.monotonic(), commit=Recorder().commit, run_id="run")
    ingress = Ingress(owner, 1000)
    points = [p for p in config.points if p.source == "mb"]

    async def no_wait(delay):
        await asyncio.sleep(0.001)

    acquirer = ModbusAcquirer(
        config.sources[0], points, config.timing, ingress,
        client_factory=client_factory, ticker=fast_ticker(0.001), sleep=no_wait,
    )  # fmt: skip
    return owner, ingress, acquirer


async def test_a_modbus_gateway_that_connects_but_never_answers_reaches_persistent_resync_after_five_cycles():
    owner, ingress, acquirer = modbus_bench(FakeModbusClient)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: owner.sources["mb"].resync_count >= 5, why="five connect-then-fail cycles")
    finally:
        await stop(task)
    s = owner.sources["mb"]
    assert s.resync_count == 5 and s.conn_gen == 5, "one count per generation that fell back into resync"
    assert owner.status()["sources"]["mb"]["persistent_resync"] is True


async def test_a_modbus_gateway_that_never_connects_has_a_failing_backoff_but_zero_resyncs():
    """The backoff counter and the resync count answer different questions; neither stands in for the other."""
    owner, ingress, acquirer = modbus_bench(lambda: FakeModbusClient(connects=False))
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: acquirer.backoff.failures >= 6, why="six failed connects")
    finally:
        await stop(task)
    assert acquirer.backoff.persistent is True
    assert owner.sources["mb"].resync_count == 0 and owner.sources["mb"].conn_gen == 0


NAMES = ["a", "b"]


@pytest.fixture
async def field():
    f = FieldUa(NAMES)
    await f.start()
    yield f
    await f.stop()


def ua_bench(field, names=NAMES, tap=None):
    points = [Point(id=n, source="ua", unit="u", node_id=field.node_id(n)) for n in names]
    config = Settings.model_validate(
        {
            "sources": [{"id": "ua", "kind": "opcua", "enabled": True, "endpoint": field.endpoint}],
            "points": [p.model_dump() for p in points],
        }
    )
    owner = Owner(config, time.time(), time.monotonic(), commit=Recorder().commit, run_id="run")
    ingress = Ingress(owner, 1000)
    if tap is not None:
        tap(ingress)

    async def no_wait(delay):
        await asyncio.sleep(0.01)

    acquirer = UaAcquirer(config.sources[0], points, config.timing, ingress, sleep=no_wait)
    owner.on_rebuild = acquirer.request_rebuild
    return owner, ingress, acquirer


async def test_an_opcua_server_that_connects_but_refuses_the_items_reaches_persistent_resync_after_five_cycles(field):
    owner, ingress, acquirer = ua_bench(field, ["a", "missing"])  # "missing" is not a node of the server
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: owner.sources["ua"].resync_count >= 5, why="five connect-then-fail cycles")
    finally:
        await stop(task)
    s = owner.sources["ua"]
    assert s.resync_count == 5 and s.conn_gen == 5
    assert owner.status()["sources"]["ua"]["persistent_resync"] is True


# ------------------------------------------------------------------------- E5: waiting start


def test_a_rebuilt_generation_does_not_borrow_the_previous_generations_last_communication():
    b = Bench("opcua")
    b.generation(1, 101.0)
    b.respond(1, 102.0)
    b.tick(108.0)
    assert b.rebuilds == [("ua", "SILENCE")], "the old subscription really was silent for 6 s"
    b.owner.begin_generation("ua", 2, *at(120.0))  # connected; the subscription is still being created
    b.rebuilds.clear()
    for m in range(121, 200):
        b.tick(float(m))
    assert b.rebuilds == [] and b.s.state == "WAITING", "no subscription to wait for yet: nothing to call silent"
    b.owner.begin_waiting("ua", 2, 200.0)
    b.tick(205.9)
    assert b.rebuilds == []
    b.tick(206.0)
    assert b.rebuilds == [("ua", "SILENCE")], "6 s from the moment the subscription started waiting"


def test_a_first_start_stays_unjudged_until_the_subscription_starts_waiting():
    b = Bench("opcua")
    b.owner.begin_generation("ua", 1, *at(101.0))  # CONNECT done; CreateSubscription takes long
    for m in range(102, 160):
        b.tick(float(m))
    assert b.rebuilds == [] and b.s.state == "WAITING" and b.count == 0
    assert b.s.alarm, "the source alarm (6 s without a response) is separate from the subscription's wait"
    b.owner.begin_waiting("ua", 1, 160.0)
    b.tick(165.9)
    assert b.rebuilds == []
    b.tick(166.0)
    assert b.rebuilds == [("ua", "SILENCE")]


def test_the_waiting_event_does_not_overwrite_a_first_response_that_beat_it():
    b = Bench("opcua")
    b.owner.begin_generation("ua", 1, *at(101.0))
    b.respond(1, 112.0)  # the first response is already in
    b.owner.begin_waiting("ua", 1, 110.0)  # the event for the earlier CreateSubscription return arrives afterwards
    assert b.s.last_comm_mono == 112.0 and b.s.last_comm_wall == at(112.0)[0], "the response time stands"
    b.tick(114.9)
    assert b.s.state == "VALID"
    b.tick(115.0)
    assert b.s.state == "EXPIRED", "3 s from the response (112), not from the waiting start (110)"
    b.tick(117.9)
    assert b.rebuilds == []
    b.tick(118.0)
    assert b.rebuilds == [("ua", "SILENCE")]


def test_the_waiting_event_of_an_old_generation_is_ignored():
    b = Bench("opcua")
    b.generation(1, 101.0)
    b.respond(1, 102.0)
    b.down(1, 103.0)
    b.owner.begin_generation("ua", 2, *at(110.0))
    b.owner.begin_waiting("ua", 1, 111.0)  # generation 1's event, late
    for m in range(112, 150):
        b.tick(float(m))
    assert b.s.wait_mono is None and b.rebuilds == [("ua", "DOWN:CONNECTION")]


async def test_the_waiting_event_follows_create_subscription_without_waiting_for_the_monitored_items(field, monkeypatch):
    """Ordered ingress: generation, then waiting (CreateSubscription has returned), and only then the items."""
    events = []

    def tap(ingress):
        for name in ("begin_generation", "begin_waiting"):
            original = getattr(ingress, name)

            def call(*args, _name=name, _original=original, **kw):
                events.append(_name)
                return _original(*args, **kw)

            setattr(ingress, name, call)

    original_items = ManagedSubscription.create_items

    async def slow_items(self, *args, **kw):
        events.append("items_start")
        await asyncio.sleep(0.4)  # the 200-item case: creating the items takes a while
        result = await original_items(self, *args, **kw)
        events.append("items_done")
        return result

    monkeypatch.setattr(ManagedSubscription, "create_items", slow_items)
    owner, ingress, acquirer = ua_bench(field, tap=tap)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: "items_done" in events, why="items created")
        await drain_until(ingress, lambda: owner.sources["ua"].state == "VALID", why="first publish")
    finally:
        await stop(task)
    assert events[:3] == ["begin_generation", "begin_waiting", "items_start"], events
    assert owner.sources["ua"].wait_mono is not None


# --------------------------------------------------------------------------- E8: alarm baseline


@pytest.mark.parametrize(
    ("protocol", "limit"), [("modbus", 10.0), ("opcua", 6.0)]
)
def test_the_source_alarm_runs_from_the_moment_acquisition_starts_not_from_the_owners_construction(protocol, limit):
    b = Bench(protocol)  # built at monotonic 100 ...
    b.owner.start(*at(130.0))  # ... but the archive took 30 s to open: acquisition begins at 130
    b.tick(131.0)
    assert not b.s.alarm, "a late start must not arrive with the whole waiting time already used"
    b.tick(130.0 + limit - 0.1)
    assert not b.s.alarm
    b.tick(130.0 + limit)
    assert b.s.alarm
    (alarm,) = b.rec.states("COMM_ALARM")
    assert alarm.detail["silent_seconds"] == pytest.approx(limit) and alarm.detail["never_responded"] is True
