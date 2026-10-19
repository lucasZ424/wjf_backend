"""R05 / Q05: a round in which only some requests are answered - pinned as the code behaves today (终版 §9, §11).

Why it matters: with more than one read request per round (devices, register kinds, address blocks) one request can go
unanswered while the others are answered.  The agreed meaning (用户 2026-10-15, written into 终版 §9, §11) is that a
timeout round is a round with NO valid protocol response at all.  A partly answered round is an OK round: its answered
inputs stay valid, the others are unavailable with their reason and logged per request, the stable 60 seconds go on and
clear the resync count, and it never counts towards the three timeout rounds in a row that reconnect.  A device's
exception answer is a valid response too.  Nothing here changes behaviour; it keeps it from being changed by accident.
Every case runs against a client and a clock the test controls, and against a real pymodbus server (not only owner input).
"""

import asyncio
import time
from dataclasses import replace
from pathlib import Path

import pytest
from acquirer_support import FakeResponse, Gate, drain_until, float_registers, stop
from owner_support import Recorder
from protocol_support import FieldModbusDevices, device_points, fast_ticker
from pymodbus.exceptions import ModbusIOException
from pymodbus.pdu import ExceptionResponse

from htsg.config import Settings, Timing, load_settings
from htsg.ingress import Ingress
from htsg.modbus import ModbusAcquirer, build_plan
from htsg.owner import Owner

OK, SILENT, EXCEPTION = FieldModbusDevices.OK, FieldModbusDevices.SILENT, FieldModbusDevices.EXCEPTION
WALL0, MONO0 = 1000.0, 100.0
QUICK = Timing(modbus_request_timeout=0.15, modbus_connect_timeout=0.3)


def at(mono):
    return WALL0 + (mono - MONO0), mono


def settings(points, port=15020, timing=None):
    return Settings.model_validate(
        {
            "sources": [{"id": "mb", "kind": "modbus", "enabled": True, "host": "127.0.0.1", "port": port}],
            "points": [p.model_dump() for p in points],
            "timing": (timing or Timing()).model_dump(),
        }
    )


def entries(owner, event):
    return [e for e in owner.commlog.entries.values() if e.event == event]


async def until(condition, seconds=10.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.02)


# ------------------------------------------------------------------------------ a scripted client


class Devices:
    """A scripted client for several devices on one connection.  `plan(n)` says what each device does in round `n`; a
    round is one request per device, in device order, so the round number is the request count over the device count."""

    def __init__(self, plan, devices=2):
        self.plan, self.devices, self.reads = plan, devices, 0
        self.on_round = None  # callable(round number), run when a round starts

    def factory(self):
        return self  # one object serves every connection: the plan counts rounds across reconnects

    async def connect(self):
        return True

    def close(self):
        pass

    async def read_holding_registers(self, address, count=1, device_id=1):
        number = self.reads // self.devices
        if self.reads % self.devices == 0 and self.on_round is not None:
            self.on_round(number)
        mode = self.plan(number)[device_id]
        self.reads += 1
        if mode == SILENT:
            raise ModbusIOException("No response received after 0 retries, continue with next request")
        if mode == EXCEPTION:
            return ExceptionResponse(3, 4)
        return FakeResponse(float_registers(count))

    read_input_registers = read_holding_registers


def constant(first, second):
    return lambda number: {1: first, 2: second}


# ------------------------------------------------------------- controlled clock: the real rounds, stamped by the test


class Rounds:
    """The real acquirer's rounds against a scripted client, handed to a real owner on a clock the test controls."""

    def __init__(self, plan):
        self.config = settings(device_points())
        self.owner = Owner(self.config, WALL0, MONO0, commit=Recorder().commit, run_id="run")
        self.acquirer = ModbusAcquirer(self.config.sources[0], self.config.points, self.config.timing, Ingress(self.owner, 100))
        self.client, self.n, self.gen = Devices(plan), 0, 0

    @property
    def s(self):
        return self.owner.sources["mb"]

    def generation(self, gen, mono):
        self.gen, self.acquirer.gen = gen, gen
        self.owner.begin_generation("mb", gen, *at(mono))

    def three_resyncs(self):
        """Three generations that fell straight back into resync, then a fourth that is connected."""
        for k in range(1, 4):
            self.generation(k, 100.0 + 10 * k)
            self.owner.source_down("mb", k, "CONNECTION", *at(101.0 + 10 * k))
        self.generation(4, 200.0)
        assert self.s.resync_count == 3

    async def round(self, mono):
        self.n += 1
        batch, _ = await self.acquirer.poll(self.client, self.n)
        wall, mono = at(mono)
        self.owner.accept(replace(batch, conn_gen=self.gen, wall=wall, mono=mono))
        self.owner.tick(wall, mono)
        return batch


async def test_partly_answered_rounds_keep_the_stable_spell_so_sixty_seconds_clear_the_resync_count():
    """Q05: the round is OK although one request went unanswered - for 75 seconds.  The spell is counted from the first
    response (second 201), so the count starts over at second 261, not before."""
    rounds = Rounds(constant(OK, SILENT))
    rounds.three_resyncs()
    for second in range(201, 276):
        batch = await rounds.round(float(second))
        assert batch.outcome == "OK" and [q.outcome for q in batch.requests] == ["OK", "TIMEOUT"]
        assert rounds.s.resync_count == (3 if second < 261 else 0), second
        assert rounds.s.state == "VALID" and not rounds.s.alarm
    points = rounds.owner.points
    assert points["d1_a"].good and points["d1_b"].good, "the answered device's inputs stay valid"
    assert [(points[p].good, points[p].reason) for p in ("d2_a", "d2_b")] == [(False, "POLL_MISSING")] * 2
    (timeout,) = entries(rounds.owner, "POLL_TIMEOUT")
    assert (timeout.scope, timeout.state, timeout.count) == ("holding/2/0+4", "ACTIVE", 75), "one running entry, per request"
    assert not entries(rounds.owner, "POLL_EXCEPTION")


async def test_rounds_without_any_valid_response_never_clear_the_resync_count():
    """Q05 control: the same 75 seconds, but after one first answer nothing answers - every round is a timeout round."""
    rounds = Rounds(lambda number: constant(OK, OK)(number) if number == 0 else constant(SILENT, SILENT)(number))
    rounds.three_resyncs()
    assert (await rounds.round(201.0)).outcome == "OK"  # the first response: the spell would count from here
    for second in range(202, 276):
        batch = await rounds.round(float(second))
        assert batch.outcome == "TIMEOUT"
        assert rounds.s.resync_count == 3, second
    assert sorted((e.scope, e.count) for e in entries(rounds.owner, "POLL_TIMEOUT")) == [("holding/1/0+4", 74), ("holding/2/0+4", 74)]


@pytest.mark.parametrize(
    ("mode1", "mode2", "outcome"), [(OK, EXCEPTION, "OK"), (EXCEPTION, EXCEPTION, "EXCEPTION")], ids=["partly", "wholly"]
)
async def test_a_device_exception_is_a_valid_response_no_timeout_and_no_alarm(mode1, mode2, outcome):
    """Q05: an exception answer proves the device is there; it clears the resync count like any answer and is no timeout."""
    rounds = Rounds(constant(mode1, mode2))
    rounds.three_resyncs()
    for second in range(201, 276):
        batch = await rounds.round(float(second))
        assert batch.outcome == outcome
        assert rounds.s.resync_count == (3 if second < 261 else 0) and not rounds.s.alarm, second
    assert not entries(rounds.owner, "POLL_TIMEOUT"), "an exception answer is not a timeout"
    assert entries(rounds.owner, "POLL_EXCEPTION")[0].reason == "EXCEPTION_CODE_4"


# ------------------------------------------------- the acquirer's own count: scripted client, stepped quickly


async def run_rounds(plan, rounds, devices=2):
    """The real acquirer, run for `rounds` offered rounds against a scripted client; returns what happened."""
    points = device_points(tuple(range(1, devices + 1)))
    config = settings(points)
    owner = Owner(config, time.time(), time.monotonic(), commit=Recorder().commit, run_id="run")
    ingress = Ingress(owner, 10_000)
    client = Devices(plan, devices)
    gate = Gate(10_000)
    acquirer = ModbusAcquirer(
        config.sources[0], config.points, config.timing, ingress, client_factory=client.factory, ticker=fast_ticker(0.001), sleep=gate
    )  # fmt: skip
    seen = []  # the acquirer's consecutive-timeout count at the start of every round
    client.on_round = lambda number: seen.append(acquirer.consecutive_timeouts)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: len(acquirer.outcomes) >= rounds, why=f"{rounds} rounds")
    finally:
        await stop(task)
    ingress.drain()
    return acquirer, owner, seen


async def test_three_rounds_in_a_row_without_any_response_reconnect_once():
    acquirer, owner, seen = await run_rounds(lambda n: {1: SILENT, 2: SILENT} if n < 3 else {1: OK, 2: OK}, 6)
    assert acquirer.outcomes[:6] == ["TIMEOUT"] * 3 + ["OK"] * 3
    assert acquirer.gen == 2 and owner.sources["mb"].resync_count == 1, "three timeout rounds: one reconnect, one resync"
    assert [e.reason for e in entries(owner, "RESYNC")] == ["DOWN:TIMEOUTS"]


async def test_an_answer_from_any_request_in_between_clears_the_count_so_there_is_never_a_reconnect():
    def plan(n):
        return {1: OK, 2: SILENT} if n % 3 == 2 else {1: SILENT, 2: SILENT}  # two silent rounds, then one half-answered, ...

    acquirer, owner, seen = await run_rounds(plan, 12)
    assert acquirer.outcomes[:12] == ["TIMEOUT", "TIMEOUT", "OK"] * 4
    assert acquirer.gen == 1 and owner.sources["mb"].resync_count == 0, "no reconnect"
    assert max(seen) == 2 and acquirer.connect_failures == 0, "the count reached two, and an answer reset it each time"


@pytest.mark.parametrize(("first", "second", "outcome"), [(OK, EXCEPTION, "OK"), (EXCEPTION, EXCEPTION, "EXCEPTION")])
async def test_exception_answers_are_not_timeouts_so_they_never_reconnect(first, second, outcome):
    acquirer, owner, seen = await run_rounds(constant(first, second), 10)
    assert set(acquirer.outcomes[:10]) == {outcome} and acquirer.gen == 1 and max(seen) == 0
    assert owner.sources["mb"].resync_count == 0 and not entries(owner, "POLL_TIMEOUT")


async def test_the_current_research_configuration_has_one_request_per_round_so_every_timeout_is_a_whole_round():
    """Q05 (单请求现行研究配置): a partly answered round cannot happen there, and the old behaviour is unchanged."""
    research = load_settings(Path(__file__).resolve().parents[1] / "config" / "research-sim.json")
    assert len(build_plan([p for p in research.points if p.source == "mb_field"])) == 1
    acquirer, owner, seen = await run_rounds(lambda n: {1: SILENT} if n < 3 else {1: OK}, 6, devices=1)
    assert acquirer.outcomes[:6] == ["TIMEOUT"] * 3 + ["OK"] * 3 and acquirer.gen == 2
    assert owner.sources["mb"].resync_count == 1


# ---------------------------------------------------------------------- a real pymodbus server with several slaves


class Live:
    """A real acquirer on a real multi-slave server, handing its rounds to a real owner (clock and ticks are real)."""

    def __init__(self, server, *, timing=QUICK, ticker=fast_ticker(0.02), devices=(1, 2), on_round=None):
        self.config = settings(device_points(devices), server.port, timing)
        self.recorder = Recorder()
        self.owner = Owner(self.config, time.time(), time.monotonic(), commit=self.recorder.commit, run_id="run")
        self.ingress = Ingress(self.owner, 10_000)
        self.rounds, self.gens = [], []  # the outcome of every offered round, and the generation it was made in
        offer = self.ingress.offer

        def tapped(batch):
            accepted = offer(batch)
            self.rounds.append(batch.outcome)
            self.gens.append(self.acquirer.gen)
            if on_round is not None:
                on_round(len(self.rounds))  # runs before the next round starts: what the slaves do next is set here
            return accepted

        self.ingress.offer = tapped

        async def quick(delay):
            await asyncio.sleep(0.01)

        extra = {} if ticker is None else {"ticker": ticker}
        self.acquirer = ModbusAcquirer(self.config.sources[0], self.config.points, timing, self.ingress, sleep=quick, **extra)

    @property
    def s(self):
        return self.owner.sources["mb"]

    async def pump(self):
        next_tick = time.monotonic() + 1
        while True:
            self.ingress.drain()
            now = time.monotonic()
            if now >= next_tick:
                self.owner.tick(time.time(), now)
                next_tick += 1
            await asyncio.sleep(0.02)

    async def __aenter__(self):
        self.tasks = [asyncio.create_task(self.acquirer.run()), asyncio.create_task(self.pump())]
        return self

    async def __aexit__(self, *exc):
        await stop(*self.tasks)


async def test_a_real_slave_that_never_answers_leaves_the_connection_alone_and_a_minute_of_it_clears_the_count():
    """Q05 on the real wire, with the real timings (0.8 s request timeout, one round a second): over a minute of rounds in
    which one of two slaves never answers.  The link is cut once first, so there is a resync count to clear."""
    server = FieldModbusDevices()
    await server.start()
    try:
        async with Live(server, timing=Timing(), ticker=None) as live:
            s = live.s
            await until(lambda: s.last_comm_mono is not None, 15)
            await server.stop()  # the link drops: the first generation ends in a resync
            await until(lambda: s.resync_count == 1, 15)
            await server.start()
            await until(lambda: s.conn_gen == 2 and s.state == "VALID" and s.stable_since is not None, 20)
            server.mode[2] = SILENT
            spell_start = s.stable_since
            while s.resync_count:
                assert time.monotonic() - spell_start < 75, "the count was not cleared"
                assert live.acquirer.consecutive_timeouts == 0 and live.acquirer.gen == 2 and s.conn_gen == 2
                assert live.owner.points["d1_a"].good and live.owner.points["d1_b"].good
                await asyncio.sleep(0.25)
            assert 60 <= time.monotonic() - spell_start < 64, "cleared by the stable spell, not before"
            assert s.state == "VALID" and not s.alarm and live.acquirer.gen == 2
            points = live.owner.points
            assert not points["d2_a"].good and points["d2_a"].reason == "POLL_MISSING" and points["d1_a"].good
            (timeout,) = [e for e in entries(live.owner, "POLL_TIMEOUT") if e.conn_gen == 2]
            assert (timeout.scope, timeout.state) == ("holding/2/0+4", "ACTIVE")
            assert timeout.count >= 55, "one running entry for the silent request, counting its rounds"
            assert {device for device, _, _ in server.requests} == {1, 2}
    finally:
        await server.stop()


async def run_schedule(rounds, after, expect_rounds):
    """A real acquirer against two real slaves whose behaviour is set per round (from the round before it ends); after the
    schedule both slaves answer.  Returns the finished `Live`, still holding its rounds."""
    server = FieldModbusDevices()
    await server.start()

    def next_round(offered):
        server.mode[1], server.mode[2] = rounds[offered] if offered < len(rounds) else after

    server.mode[1], server.mode[2] = rounds[0]
    try:
        async with Live(server, on_round=next_round) as live:
            await until(lambda: len(live.rounds) >= expect_rounds, 30)
        return live
    finally:
        await server.stop()


async def test_real_slaves_an_answer_in_between_resets_the_count_so_two_silent_rounds_never_add_up_to_a_reconnect():
    """Q05 on the real wire: two rounds of silence, then a round in which only one slave, or both, answer - over and over."""
    schedule = [(SILENT, SILENT), (SILENT, SILENT), (OK, SILENT), (OK, OK)] * 3
    live = await run_schedule(schedule, (OK, OK), len(schedule) + 2)
    assert live.rounds[: len(schedule)] == ["TIMEOUT", "TIMEOUT", "OK", "OK"] * 3
    assert set(live.gens) == {1} and live.s.resync_count == 0, "twelve rounds, three silent ones in a row never happened"


async def test_real_slaves_that_all_stay_silent_cost_the_connection_in_the_third_round():
    """Q05 on the real wire: with two requests per round and both unanswered, pymodbus 3.15.0 closes the connection itself
    once five requests in a row went unanswered (retries+3): in the third round, so that round reads CONNECTION, not
    TIMEOUT.  The acquirer's own count of three timeout rounds is not reached first; either way one reconnect follows
    (终版 §11).  Fixed as it is, so that a change of the library or of the rule shows."""
    schedule = [(SILENT, SILENT)] * 3
    live = await run_schedule(schedule, (OK, OK), 6)
    assert live.rounds[:6] == ["TIMEOUT", "TIMEOUT", "CONNECTION", "OK", "OK", "OK"]
    assert live.gens[:3] == [1, 1, 1] and live.gens[3] == 2 and live.s.resync_count == 1, "one reconnect, one resync"


@pytest.mark.parametrize(("first", "second", "outcome"), [(OK, EXCEPTION, "OK"), (EXCEPTION, EXCEPTION, "EXCEPTION")])
async def test_real_slaves_that_answer_with_an_exception_are_no_timeout_and_no_alarm(first, second, outcome):
    server = FieldModbusDevices()
    server.mode[1], server.mode[2] = first, second
    await server.start()
    try:
        async with Live(server) as live:
            await until(lambda: len(live.rounds) >= 8, 20)
            assert set(live.rounds[:8]) == {outcome} and set(live.gens) == {1}
            assert live.acquirer.consecutive_timeouts == 0 and not live.s.alarm and live.s.resync_count == 0
            assert not entries(live.owner, "POLL_TIMEOUT")
            (failure,) = [e for e in entries(live.owner, "POLL_EXCEPTION") if e.scope == "holding/2/0+4"]
            assert failure.reason == "EXCEPTION_CODE_4" and failure.state == "ACTIVE"
            assert bool(live.owner.points["d1_a"].good) == (first == OK)
    finally:
        await server.stop()


async def test_a_real_single_slave_that_never_answers_is_a_whole_round_timeout_three_of_them_reconnect():
    server = FieldModbusDevices(devices=(1,))
    server.mode[1] = SILENT

    def next_round(offered):
        server.mode[1] = SILENT if offered < 3 else OK

    await server.start()
    try:
        async with Live(server, devices=(1,), on_round=next_round) as live:
            await until(lambda: len(live.rounds) >= 5, 20)
            assert live.rounds[:5] == ["TIMEOUT"] * 3 + ["OK"] * 2
            assert live.gens[:3] == [1, 1, 1] and live.gens[3] == 2 and live.s.resync_count == 1
    finally:
        await server.stop()
