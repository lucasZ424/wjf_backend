"""D44f: the communication state against REAL local Modbus and OPC UA servers, and against controlled clocks.

Why it matters: the facts in the log and the two counters are only worth something if they are what really happens on
the wire - a refused connection, a device that answers with a protocol exception, a server that goes away and comes back
within the alarm time, a subscription the server refuses - and if the clocks the page shows can never be mistaken for
evidence of anything.  Where a real library hides the reason (pymodbus returns False), the log says exactly that.

Real-server Modbus tests stop the server before the acquirer task is cancelled and park the task between attempts (in
its backoff wait), because pymodbus 3.15.0 turns a cancel that lands inside a read into a retry (see the execution record,
section 2: a known, separate problem that is not fixed here).
"""

import asyncio
import time

import pytest
from acquirer_support import Gate, ScriptedModbus, drain_until, stop
from owner_support import Recorder, small_config
from protocol_support import FieldModbus, FieldUa, bridge_points, fast_ticker
from pymodbus.constants import ExcCodes
from test_comm_facts import attempt_entries, entries, events, ua_bench, ua_points
from test_resync_health import Bench, at
from test_ua_channel import publish

from htsg import records as r
from htsg.config import Settings, Timing
from htsg.ingress import Ingress
from htsg.modbus import ModbusAcquirer
from htsg.owner import Owner
from htsg.ua import ManagedSubscription, UaChannel

# the request timeout is shorter than the connect timeout, so the library gives up (returns False) before our own limit does
FAST = Timing(modbus_request_timeout=0.15, modbus_connect_timeout=0.9)


def real_modbus(server, timing=FAST):
    points = bridge_points()
    config = Settings.model_validate(
        {
            "sources": [server.source().model_dump()],
            "points": [p.model_dump() for p in points],
            "timing": timing.model_dump(),
        }
    )
    owner = Owner(config, time.time(), time.monotonic(), commit=Recorder().commit, run_id="run")
    ingress = Ingress(owner, 10_000)

    def build(attempts, ticker=None):
        gate = Gate(attempts)
        acquirer = ModbusAcquirer(config.sources[0], points, timing, ingress, ticker=ticker or fast_ticker(0.02), sleep=gate)
        return acquirer, gate

    return owner, ingress, build


class HeldTicker:
    """fast_ticker that stops ONCE between two rounds: the first time `pause_when` holds after a round has ended, the next
    tick waits for `go`.  `parked` says the acquirer is there - its round returned, no request is on the wire."""

    def __init__(self, period=0.02):
        self.period, self.pause_when, self.parked, self.go = period, None, asyncio.Event(), asyncio.Event()

    async def __call__(self):
        n = 0
        while True:
            yield n
            n += 1
            if self.pause_when and self.pause_when():
                self.pause_when = None
                self.parked.set()
                await self.go.wait()
            await asyncio.sleep(self.period)


# ================================================================================ Modbus, real server


@pytest.mark.parametrize(
    ("timing", "reason", "detail"),
    [
        (Timing(modbus_request_timeout=0.2, modbus_connect_timeout=0.9), "LIBRARY_NO_REASON", "连接失败，库未提供原因"),
        (Timing(), "APP_CONNECT_TIMEOUT", "建连调用0.8秒内未返回"),
    ],
    ids=["library-returns-False", "our-own-timeout"],
)
async def test_a_connect_that_cannot_succeed_is_reported_as_what_the_real_library_lets_us_know(timing, reason, detail):
    """S4: nobody listens.  The library either returns False (no reason given - say so) or does not return within our
    0.8 s (say exactly that); never a guessed cause such as 'refused' or 'device off'."""
    server = FieldModbus()  # a free port, never started
    owner, ingress, build = real_modbus(server, timing)
    acquirer, gate = build(2)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: gate.blocked, why="two attempts")
    finally:
        await stop(task)
    ingress.drain()
    (entry,) = attempt_entries(owner, "CONNECT")
    assert (entry.reason, entry.detail, entry.count, entry.conn_gen) == (reason, detail, 2, None)
    assert acquirer.connect_failures == 2 and owner.sources["mb"].resync_count == 0 and owner.sources["mb"].conn_gen == 0


async def test_a_device_exception_is_logged_with_its_code_and_exception_ok_exception_is_two_entries():
    server = FieldModbus()
    await server.start()
    owner, ingress, build = real_modbus(server)
    acquirer, gate = build(10_000)
    task = asyncio.create_task(acquirer.run())
    exceptions = lambda: [e for e in owner.commlog.entries.values() if e.event == "POLL_EXCEPTION"]  # noqa: E731
    try:
        await drain_until(ingress, lambda: acquirer.outcomes[-3:] == ["OK"] * 3, why="healthy rounds")
        server.exception = True  # the device answers every request with DEVICE_FAILURE
        await drain_until(ingress, lambda: exceptions() and exceptions()[0].count >= 2, why="two exception rounds")
        comm_during = owner.sources["mb"].last_comm_mono
        server.exception = False
        await drain_until(ingress, lambda: exceptions()[0].state == "ENDED", why="an answered round")
        server.exception = True
        await drain_until(ingress, lambda: len(exceptions()) == 2, why="exceptions again")
    finally:
        await server.stop()
        await stop(task)
    ingress.drain()
    first, second = exceptions()
    code = ExcCodes.DEVICE_FAILURE.value
    assert first.reason == second.reason == f"EXCEPTION_CODE_{code}"
    assert (first.state, first.end_reason, first.count >= 2) == ("ENDED", "SUCCESS", True)
    assert second.state == "ACTIVE" and second.seq > first.seq and first.ended_wall > first.last_wall
    s = owner.sources["mb"]
    assert s.state == "VALID" and not s.alarm and s.resync_count == 0 and acquirer.connect_failures == 0
    assert comm_during is not None and not [e for e in events(owner) if e in ("RESYNC", "COMM_ALARM", "DATA_EXPIRED")], (
        "an answering device is alive: its exception is a failed round, not a disconnect"
    )


async def test_a_device_that_connects_but_never_answers_reaches_five_resyncs_and_never_a_connect_failure():
    """P2 on the real stack: connected, three timed-out rounds, back into resync - five times over."""
    server = FieldModbus()
    server.delay = 1.0  # every request is held longer than the 0.15 s request timeout
    await server.start()
    owner, ingress, build = real_modbus(server)
    acquirer, gate = build(5)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: gate.blocked, timeout=60, why="five cycles")  # parked in its backoff wait
    finally:
        await server.stop()
        await stop(task)
    ingress.drain()
    s = owner.sources["mb"]
    assert (s.resync_count, s.conn_gen, acquirer.connect_failures) == (5, 5, 0)
    assert attempt_entries(owner, "CONNECT") == [], "a timed-out round is not a failed connect attempt"
    assert [(e.count, e.conn_gen) for e in owner.commlog.entries.values() if e.event == "POLL_TIMEOUT"] == [(3, g) for g in range(1, 6)]
    assert owner.status()["sources"]["mb"]["persistent_resync"] is True


async def test_five_refused_reconnects_after_a_real_disconnect_count_five_with_one_resync_and_the_return_is_unalarmed():
    """The disconnect is made between two rounds and the next round starts only once the client has seen the connection
    close: a request on the wire when the server goes would time out first (a true POLL_TIMEOUT before the RESYNC), and
    that is a different case from the one this test pins down."""
    server = FieldModbus()
    await server.start()
    owner, ingress, build = real_modbus(server)
    ticker = HeldTicker()
    acquirer, gate = build(6, ticker)  # one session, then five failed connects
    clients, make = [], acquirer.client_factory
    acquirer.client_factory = lambda: clients.append(make()) or clients[-1]  # the real client, kept to look at
    ticker.pause_when = lambda: acquirer.outcomes[-3:] == ["OK"] * 3
    task = asyncio.create_task(acquirer.run())
    s = owner.sources["mb"]
    try:
        await drain_until(ingress, ticker.parked.is_set, why="a live connection, parked after three answered rounds")
        await server.stop()
        await drain_until(ingress, lambda: not clients[0].connected, why="the client has seen the connection close")
        ticker.go.set()
        await drain_until(ingress, lambda: gate.blocked, timeout=60, why="the failed reconnects")
        ingress.drain()  # what the acquirer reported just before it parked
        assert (acquirer.connect_failures, s.resync_count, s.conn_gen) == (5, 1, 1)
        (attempts,) = attempt_entries(owner, "CONNECT")
        assert (attempts.count, attempts.reason, attempts.state) == (5, "LIBRARY_NO_REASON", "ACTIVE"), "kept after the disconnect"
        await server.start()  # the device is back after a few seconds - far inside the 10 s alarm
        gate.release.set()
        await drain_until(ingress, lambda: s.state == "VALID" and s.conn_gen == 2, timeout=30, why="back")
    finally:
        await server.stop()
        await stop(task)
    ingress.drain()
    assert acquirer.connect_failures == 0 and attempts.end_reason == "CONNECT_OK"
    assert events(owner) == ["NEW_GENERATION", "FIRST_COMM", "RESYNC", "NEW_GENERATION", "COMM_RECOVERED"], (
        "recovery without any alarm is still recorded, apart from the first communication"
    )
    (recovered,) = entries(owner, "COMM_RECOVERED")
    assert 0.5 < recovered.as_dict()["silent_seconds"] < 10 and not s.alarm


async def test_the_connect_failure_count_does_not_depend_on_the_diagnostic_queue_or_the_log():
    """R1: the count lives on the acquirer.  A full diagnostic allowance (nothing reaches the log) changes nothing."""
    script = ScriptedModbus([False])
    server = FieldModbus()
    owner, _, _ = real_modbus(server)
    ingress = Ingress(owner, 10_000, diag_capacity=0)  # every diagnostic fact is refused
    gate = Gate(5)
    acquirer = ModbusAcquirer(
        owner.config.sources[0], bridge_points(), owner.config.timing, ingress,
        client_factory=script.factory, ticker=fast_ticker(0.001), sleep=gate,
    )  # fmt: skip
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: gate.blocked, why="five failed attempts")
    finally:
        await stop(task)
    ingress.drain()
    assert acquirer.connect_failures == 5, "counted by the acquirer itself"
    assert ingress.diag_dropped == 5 and owner.commlog.entries == {} and owner.commlog.diag_lost == 5
    assert owner.health["app_loss"] == 0 and owner.sources["mb"].resync_count == 0, "and no application loss, no resync"


# =============================================================================== OPC UA, real server

NAMES = ["a"]


@pytest.fixture
async def field():
    f = FieldUa(NAMES)
    await f.start()
    yield f
    await f.stop()


FAST_UA = Timing(ua_request_timeout=1.0)


async def test_five_failed_connects_before_any_connection_count_five_with_no_resync():
    nobody = FieldUa(NAMES)  # a free port, never started
    nobody.index = 2
    owner, ingress, build = ua_bench(nobody.endpoint, ua_points(nobody, "a"))
    acquirer, gate = build(5)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: gate.blocked, timeout=60, why="five refused connects")
    finally:
        await stop(task)
    ingress.drain()
    s = owner.sources["ua"]
    assert (acquirer.connect_failures, s.resync_count, s.conn_gen) == (5, 0, 0)
    assert sum(e.count for e in attempt_entries(owner, "CONNECT")) == 5 and attempt_entries(owner, "SUBSCRIBE") == []


async def test_five_failed_connects_after_a_real_disconnect_count_five_while_the_resync_count_stays_one(field):
    owner, ingress, build = ua_bench(field.endpoint, ua_points(field, "a"))
    acquirer, gate = build(6)  # one session, then five failed connects
    task = asyncio.create_task(acquirer.run())
    s = owner.sources["ua"]
    try:
        await drain_until(ingress, lambda: s.state == "VALID", why="a live connection")
        await field.stop()
        await drain_until(ingress, lambda: gate.blocked, timeout=90, why="the failed reconnects")
    finally:
        await stop(task)
    ingress.drain()
    assert (acquirer.connect_failures, s.resync_count, s.conn_gen) == (5, 1, 1)
    assert sum(e.count for e in attempt_entries(owner, "CONNECT")) == 5


async def test_a_connect_that_succeeds_clears_the_count_and_a_refused_subscription_after_it_does_not_bring_it_back(field):
    points = ua_points(field, "missing")  # every item refused: the subscription fails
    owner, ingress, build = ua_bench(field.endpoint, points)
    acquirer, gate = build(3)
    acquirer.connect_failures = 4  # as if four connects had failed before
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: gate.blocked, timeout=60, why="three cycles")
    finally:
        await stop(task)
    ingress.drain()
    assert acquirer.connect_failures == 0, "connect() returned: cleared at once, not after a stable minute"
    assert attempt_entries(owner, "CONNECT") == [] and sum(e.count for e in attempt_entries(owner, "SUBSCRIBE")) == 3


async def test_a_first_value_with_a_bad_quality_is_communication_and_stays_visible_as_a_bad_reading(field):
    await field.set("a", 12.5, good=False)  # the server publishes it Bad from the very first notification
    owner, ingress, build = ua_bench(field.endpoint, ua_points(field, "a"))
    acquirer, gate = build(10_000)
    task = asyncio.create_task(acquirer.run())
    s = owner.sources["ua"]
    try:
        await drain_until(ingress, lambda: owner.points["a"].obs is not None, why="the first notification")
    finally:
        await stop(task)
    ingress.drain()
    row = owner.communication_inputs()[0]
    assert row["quality"] == "BAD" and row["status_code"] >> 30 == 0b10 and row["valid"] is False, "a Bad status, kept whole"
    assert row["value"] is None, "this server sends no value with a Bad status: left empty, not filled with a zero or the old 12.5"
    assert s.state == "VALID" and s.resync_count == 0 and events(owner) == ["NEW_GENERATION", "FIRST_COMM"], (
        "the link is fine; the reading is not"
    )


async def test_waiting_starts_when_create_subscription_returns_and_the_failure_ends_only_when_open_subscription_does(field, monkeypatch):
    points = ua_points(field, "a", "missing")
    owner, ingress, build = ua_bench(field.endpoint, points)
    acquirer, gate = build(10_000)
    acquirer.points = points[1:]  # only the unknown node: nothing to subscribe to, the subscription fails
    gate.on_call = lambda n: setattr(acquirer, "points", points) if n == 1 else None  # fixed after the first failure
    hold, calls, original = asyncio.Event(), [], ManagedSubscription.create_items

    async def controlled(self, *args, **kwargs):
        calls.append(1)
        if len(calls) == 2:  # the second cycle: the subscription exists, its items are being created ... for a while
            await hold.wait()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(ManagedSubscription, "create_items", controlled)
    task = asyncio.create_task(acquirer.run())
    s = owner.sources["ua"]
    try:
        await drain_until(ingress, lambda: len(calls) == 2 and s.conn_gen == 2 and s.wait_mono is not None, why="items held")
        (failure,) = attempt_entries(owner, "SUBSCRIBE")
        assert s.state == "WAITING", "waiting for the first publish has begun (CreateSubscription returned) ..."
        assert (failure.state, failure.count) == ("ACTIVE", 1), "... but the subscription is not yet open: the earlier failure goes on"
        hold.set()
        await drain_until(ingress, lambda: failure.state == "ENDED", why="open_subscription returned")
    finally:
        hold.set()
        await stop(task)
    assert failure.end_reason == "SUBSCRIBE_OK"


def library_level(first):
    """An owner with one OPC UA generation whose notifications arrive through the real channel class."""
    config = small_config()
    payload = config.model_dump(mode="json")
    payload["sources"][0]["enabled"] = False
    config = type(config).model_validate(payload)
    owner = Owner(config, 1000.0, 100.0, commit=Recorder().commit, run_id="run")
    ingress = Ingress(owner, 100)
    ingress.begin_generation("ua", 1, 1000.0, 100.0)
    handles = {1: next(p for p in config.points if p.id == "ti")}
    clock = [1001.0, 101.0]
    channel = UaChannel("ua", 1, ingress, handles, clock=(lambda: clock[0], lambda: clock[1]))
    channel.on_publish(first)
    ingress.drain()
    return owner


def test_a_first_message_that_is_only_a_keep_alive_is_the_first_communication():
    owner = library_level(publish(1))  # no notification data at all
    assert events(owner) == ["NEW_GENERATION", "FIRST_COMM"] and owner.sources["ua"].state == "VALID"
    assert owner.points["ti"].obs is None, "a keep-alive is not an observation"


def test_a_first_message_with_an_uncertain_value_is_communication_and_the_value_is_not_made_good():
    owner = library_level(publish(1, [(1, 480.0, 0x40920000)]))  # Uncertain
    row = next(x for x in owner.communication_inputs() if x["point_id"] == "ti")
    assert events(owner) == ["NEW_GENERATION", "FIRST_COMM"]
    assert row["quality"] == "UNCERTAIN" and row["valid"] is False and row["value"] == 480.0


# ============================================================================ controlled clocks, nothing to judge


def test_a_frozen_source_timestamp_is_shown_as_it_is_and_never_makes_the_source_old_or_dead():
    """Dead-point and freeze detection are out of scope: the timestamp judges nothing."""
    b = Bench("opcua")
    b.generation(1, 101.0)
    stamp = 1_700_000_000.0
    for k in range(1800):  # an hour; every notification carries the very same source time, some with new values
        mono = 102.0 + 2 * k
        item = r.Item("ti", 500.0 + (k % 3), r.GOOD, 0, stamp)
        batch = r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, *at(mono), (item,) if k % 5 == 0 else (), publish_seq=k + 1)
        b.owner.accept(batch)
        b.tick(mono)
    assert events(b.owner) == ["NEW_GENERATION", "FIRST_COMM"], "nothing happened, however long the timestamp stood still"
    row = next(x for x in b.owner.communication_inputs() if x["point_id"] == "ti")
    assert row["source_time"] == stamp and row["valid"] is True and b.s.state == "VALID" and not b.s.alarm
    # a timestamp from 1970 or from the far future is shown too, and changes nothing
    item = r.Item("ti", 1.0, r.GOOD, 0, 1.0)
    b.owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, *at(5000.0), (item,), publish_seq=99_999))
    row = next(x for x in b.owner.communication_inputs() if x["point_id"] == "ti")
    assert row["source_time"] == 1.0 and row["received_time"] == at(5000.0)[0] and row["valid"] is True


def test_a_step_of_the_wall_clock_neither_reorders_the_log_nor_changes_a_duration():
    b = Bench("modbus")
    log = b.owner.commlog
    log.attempt_failed("mb", "CONNECT", "REFUSED", 5000.0, 200.0)
    log.attempt_failed("mb", "CONNECT", "REFUSED", 5001.0, 201.0)
    log.attempt_failed("mb", "CONNECT", "REFUSED", 1500.0, 205.0)  # the wall clock is put back by an hour
    log.event("mb", 1, "NEW_GENERATION", "CONNECT", 1501.0, 206.0)
    snapshot = log.snapshot()["entries"]
    assert [e["seq"] for e in snapshot] == [1, 2], "owner order, not clock order"
    first = snapshot[0]
    assert first["count"] == 3 and first["first_failure"] > first["last_failure"], "the labels may run backwards ..."
    assert first["span_seconds"] == pytest.approx(5.0), "... durations come from the monotonic clock and do not"
    assert snapshot[1]["found_time"] < first["found_time"]


def test_archive_gaps_clock_jumps_and_watermarks_are_not_communication_log_entries():
    """終版 §15.1: these have their own homes (self-check, history); the log only holds communication."""
    refuse = {"on": False}
    recorder = Recorder()

    def commit(group):
        recorder.commit(group)
        return False if refuse["on"] else None  # False: the queue is full

    owner = Owner(small_config(), 1000.0, 100.0, commit=commit, run_id="run")
    owner.begin_generation("ua", 1, *at(101.0))
    owner.begin_generation("mb", 1, *at(101.0))
    owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, *at(102.0), (r.Item("ti", 500.0),), publish_seq=1))
    for m in range(103, 125):  # watermarks every 10 s
        owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, *at(float(m))))
        owner.accept(r.InputBatch("mb", 1, r.KIND_MODBUS_POLL, *at(float(m)), (r.Item("b1", 0.5),), tick=m, poll_id=str(m), outcome="OK"))
        owner.tick(*at(float(m)))
    refuse["on"] = True  # the archive stops taking groups: a gap begins
    owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, *at(125.0), (r.Item("ti", 501.0),), publish_seq=2))
    owner.tick(1000.0 + 25 + 3600, 125.0 + 1.0)  # ... and the wall clock jumps an hour
    kinds = {s.kind for g in recorder.commits for s in g.states}
    assert {"WATERMARK", "CLOCK_JUMP"} <= kinds and owner.sources["ua"].archive == "LOST", "they did happen"
    assert set(events(owner)) <= {"NEW_GENERATION", "FIRST_COMM"}, events(owner)
    assert "GAP" not in events(owner) and "WATERMARK" not in events(owner) and "CLOCK_JUMP" not in events(owner)
