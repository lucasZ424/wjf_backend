"""D12-D13: Modbus read plan, planned rounds and failure classification against a real local server.

Why it matters: a failing round must never be filled from the previous one, and an ordinary timeout,
a device exception or an extreme value must not be mistaken for a lost connection (终版 §9, §11).
"""

import asyncio
import contextlib
import struct
import time

import pytest
from protocol_support import FieldModbus, bridge_points, fast_ticker

from htsg import records as r
from htsg.config import Point, Settings, Timing
from htsg.ingress import Ingress
from htsg.modbus import ModbusAcquirer, build_plan, decode_span
from htsg.owner import Owner


def point(pid, address, device=1, kind="holding", dtype="float32", **kw):
    return Point(id=pid, source="mb", unit="u", address=address, device_id=device, register_kind=kind,
                 data_type=dtype, **kw)  # fmt: skip


# ----------------------------------------------------------------------- D12 plan


def test_eight_bridges_are_one_sixteen_register_request():
    (span,) = build_plan(bridge_points())
    assert (span.device_id, span.kind, span.address, span.count) == (1, "holding", 0, 16)
    assert [offset for _, offset in span.members] == [0, 2, 4, 6, 8, 10, 12, 14]


def test_plan_never_crosses_devices_kinds_gaps_or_the_request_limit():
    plan = build_plan(
        [point("a", 0), point("b", 2, device=2), point("c", 4, kind="input"), point("d", 10), point("e", 12)]
    )
    assert [(s.device_id, s.kind, s.address, s.count) for s in plan] == [
        (1, "holding", 0, 2),
        (1, "holding", 10, 4),
        (1, "input", 4, 2),
        (2, "holding", 2, 2),
    ], "a gap, another device or another register kind starts a new request"
    wide = [point(f"w{i}", 2 * i) for i in range(70)]  # 140 registers > 125
    sizes = [s.count for s in build_plan(wide)]
    assert max(sizes) <= 125 and sum(sizes) == 140
    # nothing outside the configured registers is ever requested
    covered = {a for s in plan for a in range(s.address, s.address + s.count)}
    assert 6 not in covered and 8 not in covered


def test_decode_follows_each_points_byte_and_word_order():
    value = 12.5
    big = list(struct.unpack(">HH", struct.pack(">f", value)))
    words_swapped = big[::-1]
    byte_swapped = [int.from_bytes(w.to_bytes(2, "big"), "little") for w in big]
    points = [
        point("be", 0),
        point("ws", 2, word_order="little"),
        point("bs", 4, byte_order="little"),
        point("i16", 6, dtype="int16", scale=0.5, offset=1),
    ]
    (span,) = build_plan(points)
    out = {p.id: (v, q) for p, v, q in decode_span(span, [*big, *words_swapped, *byte_swapped, 0xFFFE])}
    assert out["be"] == (value, r.GOOD) and out["ws"] == (value, r.GOOD) and out["bs"] == (value, r.GOOD)
    assert out["i16"] == (-2 * 0.5 + 1, r.GOOD)


# ------------------------------------------------------------------ rounds, live


class Bench:
    """One acquirer, an ordered ingress and an owner, all on a real local Modbus server."""

    def __init__(self, server, timing, points=None):
        self.points = points or bridge_points()
        self.server = server
        derived = [
            {"id": p.id.replace("bridge", "strain"), "inputs": [p.id], "operation": "bridge_strain", "unit": "με"}
            for p in self.points
            if p.id.startswith("bridge_")
        ]
        self.config = Settings.model_validate(
            {
                "sources": [
                    {"id": "mb", "kind": "modbus", "enabled": True, "host": "127.0.0.1", "port": server.port}
                ],
                "points": [p.model_dump() for p in self.points],
                "derived": derived,
                "timing": timing.model_dump(),
            }
        )
        self.commits = []
        self.owner = Owner(self.config, time.time(), time.monotonic(), commit=self.commits.append, run_id="t")
        self.ingress = Ingress(self.owner, 1000)
        self.events = []
        for name in ("begin_generation", "source_down"):
            original = getattr(self.ingress, name)
            setattr(self.ingress, name, self._tap(name, original))
        self.acquirer = ModbusAcquirer(
            self.config.sources[0], self.points, timing, self.ingress,
            ticker=fast_ticker(), sleep=self._no_wait,
        )  # fmt: skip
        self.task = None
        self.pump_task = None

    def _tap(self, name, original):
        def call(*args, **kw):
            self.events.append((name, args[1], args[2] if name == "source_down" else None))
            return original(*args, **kw)

        return call

    async def _no_wait(self, delay):
        await asyncio.sleep(0.01)

    async def pump(self):
        while True:
            self.ingress.drain()
            await asyncio.sleep(0.005)

    async def __aenter__(self):
        self.task = asyncio.create_task(self.acquirer.run())
        self.pump_task = asyncio.create_task(self.pump())
        return self

    async def __aexit__(self, *exc):
        for task in (self.task, self.pump_task):
            task.cancel()
        await asyncio.gather(self.task, self.pump_task, return_exceptions=True)

    async def until(self, condition, timeout=10.0):
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline, "condition not reached"
            await asyncio.sleep(0.01)


@pytest.fixture
async def server():
    s = FieldModbus()
    await s.start()
    yield s
    await s.stop()


FAST = Timing(modbus_request_timeout=0.15, modbus_connect_timeout=0.3)


async def test_a_round_is_one_request_with_one_poll_id_and_per_response_times(server):
    spaced = [point("a", 0), point("b", 2), point("c", 8), point("d", 10)]  # a gap -> two requests
    server.values = [0.1, 0.2, 0.0, 0.0, 0.3, 0.4, 0.0, 0.0]
    async with Bench(server, FAST, spaced) as bench:
        await bench.until(lambda: bench.owner.health.get("rejected_items") is not None and bench.commits)
        await bench.until(lambda: any(g.observations for g in bench.commits))
    group = next(g for g in bench.commits if g.observations)
    assert {o.poll_id for o in group.observations} == {group.observations[0].poll_id}
    assert {o.response_id for o in group.observations} == {f"{group.observations[0].poll_id}/0", f"{group.observations[0].poll_id}/1"}
    assert [o.value for o in group.observations] == pytest.approx([0.1, 0.2, 0.3, 0.4])
    first, second = group.observations[0], group.observations[-1]
    assert second.received_mono >= first.received_mono, "each response keeps its own receive time"
    assert (0, 4) in server.requests and (8, 4) in server.requests


async def test_the_eight_bridge_layout_is_read_with_a_single_request_per_round(server):
    async with Bench(server, FAST) as bench:
        await bench.until(lambda: len(server.requests) >= 3)
    assert set(server.requests) == {(0, 16)}


# ----------------------------------------------------------- D13 failures and recovery


async def test_the_client_uses_the_specified_timeouts_and_no_retry_and_no_library_reconnect():
    acquirer = ModbusAcquirer(
        Settings.model_validate(
            {"sources": [{"id": "mb", "kind": "modbus", "enabled": True, "host": "127.0.0.1", "port": 1}]}
        ).sources[0],
        bridge_points(), Timing(), Ingress(Owner.__new__(Owner), 1),
    )  # fmt: skip
    client = acquirer.client_factory()
    assert client.comm_params.timeout_connect == 0.8 and client.ctx.retries == 0
    assert not client.comm_params.reconnect_delay, "the library must not reconnect by itself"


async def test_a_slow_device_costs_one_timeout_of_0_8s_and_is_not_retried_inside_the_round():
    s = FieldModbus()
    await s.start()
    try:
        s.delay = 3.0
        acquirer = ModbusAcquirer(s.source(), bridge_points(), Timing(), Ingress(Owner.__new__(Owner), 1))
        client = acquirer.client_factory()
        assert await client.connect()
        started = time.monotonic()
        batch, outcome = await acquirer.poll(client, 7)
        elapsed = time.monotonic() - started
        client.close()
    finally:
        s.delay = 0
        await s.stop()
    assert outcome == "TIMEOUT" and batch.items == ()
    assert 0.7 < elapsed < 1.3, elapsed
    assert len(s.requests) == 1, "zero retries inside the round"


async def test_one_timeout_keeps_the_connection_three_in_a_row_reconnect(server):
    """Timeouts are made by withholding requests exactly - one, then three - and not by delaying every answer for a while: a
    late answer is tagged by the pymodbus server with the id of the NEWEST request and could pass as the answer to one nobody
    answered, so the number of timeouts depended on the moment the test happened to switch the delay off again."""
    async with Bench(server, FAST) as bench:
        await bench.until(lambda: bench.acquirer.outcomes[-3:] == ["OK"] * 3)
        server.withhold = 1
        await bench.until(lambda: "TIMEOUT" in bench.acquirer.outcomes)
        await bench.until(lambda: bench.acquirer.outcomes[-2:] == ["OK", "OK"])
        assert bench.acquirer.gen == 1 and not bench.events[1:], "a single timeout keeps the live connection"
        server.withhold = 3
        await bench.until(lambda: bench.acquirer.gen == 2)
        await bench.until(lambda: bench.acquirer.outcomes[-2:] == ["OK", "OK"])
    assert ("source_down", 1, "TIMEOUTS") in bench.events
    assert bench.acquirer.outcomes.count("TIMEOUT") == 4, "one, then three in a row - exactly the ones that were withheld"


async def test_device_exceptions_and_extreme_values_are_not_a_network_disconnect(server):
    async with Bench(server, FAST) as bench:
        await bench.until(lambda: bench.acquirer.outcomes[-2:] == ["OK", "OK"])
        server.exception = True
        await bench.until(lambda: bench.acquirer.outcomes[-6:] == ["EXCEPTION"] * 6)
        s = bench.owner.sources["mb"]
        assert bench.acquirer.gen == 1 and s.last_outcome == "EXCEPTION"
        before = s.last_comm_mono
        await bench.until(lambda: s.last_comm_mono > before)
        assert not s.alarm, "an answering device is alive: exceptions keep the communication time moving"
        server.exception = False
        server.values[0] = 99.0  # beyond the +-30 mV/V extreme limit
        await bench.until(lambda: bench.owner.points["bridge_1"].obs and bench.owner.points["bridge_1"].obs.value == 99.0)
        assert bench.owner.points["bridge_1"].obs.extreme and bench.acquirer.gen == 1
        assert bench.owner.results["strain_1"].current.reason == "EXTREME_INPUT"


async def test_a_dropped_connection_is_a_definite_disconnect_and_recovers_on_a_new_generation(server):
    async with Bench(server, FAST) as bench:
        await bench.until(lambda: bench.acquirer.outcomes[-2:] == ["OK", "OK"])
        await server.stop()
        await bench.until(lambda: ("source_down", 1, "CONNECTION") in bench.events)
        await bench.until(lambda: bench.owner.sources["mb"].state == "RESYNC", timeout=1.0)
        assert bench.owner.results["strain_1"].current.reason == "SOURCE_INVALID", "invalid at once, no timer"
        await server.start()
        await bench.until(lambda: bench.acquirer.gen == 2 and bench.acquirer.outcomes[-2:] == ["OK", "OK"])
        assert bench.owner.sources["mb"].state == "VALID" and bench.owner.sources["mb"].conn_gen == 2
    with contextlib.suppress(Exception):
        await server.stop()


def test_the_ten_second_alarm_runs_from_the_last_response_not_from_reconnect_attempts():
    config = Settings.model_validate(
        {
            "sources": [{"id": "mb", "kind": "modbus", "enabled": True, "host": "127.0.0.1", "port": 15020}],
            "points": [p.model_dump() for p in bridge_points(1)],
        }
    )
    owner = Owner(config, 1000.0, 100.0, run_id="t")
    owner.begin_generation("mb", 1, 1000.0, 100.0)
    owner.accept(r.InputBatch("mb", 1, r.KIND_MODBUS_POLL, 1001.0, 101.0, (r.Item("bridge_1", 0.5),), tick=1))
    owner.source_down("mb", 1, "CONNECTION", 1002.0, 102.0)
    for gen, mono in ((2, 104.0), (3, 107.0), (4, 109.0)):  # reconnect attempts that never get an answer
        owner.begin_generation("mb", gen, 1000.0 + mono - 100, mono)
        owner.source_down("mb", gen, "CONNECT_FAILED", 1000.0 + mono - 100, mono)
        owner.tick(1000.0 + mono - 100, mono)
        assert not owner.sources["mb"].alarm, mono
    owner.tick(1010.9, 110.9)
    assert not owner.sources["mb"].alarm, "9.9 s after the last response"
    owner.tick(1011.0, 111.0)
    assert owner.sources["mb"].alarm, "10 s after the last response, although the last attempt was 2 s ago"
