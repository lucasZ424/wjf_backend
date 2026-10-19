"""Modbus acquisition: one logical round per planned tick (终版 §3.3, §9, §11).

A round reads the configured registers in as few requests as the layout allows, stamps every
response with its own receive time, and ends as exactly one InputBatch.  A round that fails is
reported as a failed round, never filled from the previous one.
"""

from __future__ import annotations

import asyncio
import math
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import records as rec
from .commlog import clean_detail
from .config import Point, Source, Timing
from .recovery import Backoff
from .timing import every_second

WIDTH = {"uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2, "float64": 4}
MAX_REGISTERS = 125  # Modbus limit for one read request


def decode_registers(registers: list[int], point: Point):
    """The engineering value of one point from its registers, or (None, "BAD") for an undecodable one."""
    words = list(registers)
    if point.word_order == "little":
        words.reverse()
    raw = b"".join(x.to_bytes(2, point.byte_order) for x in words)
    fmt = {"uint16": "H", "int16": "h", "uint32": "I", "int32": "i", "float32": "f", "float64": "d"}[
        point.data_type
    ]
    value = struct.unpack(">" + fmt, raw)[0]
    if not math.isfinite(value) or value in point.invalid_values:
        return None, "BAD"
    scaled = value * point.scale + point.offset
    return (scaled, "GOOD") if math.isfinite(scaled) else (None, "BAD")


@dataclass(frozen=True, slots=True)
class Span:
    """One read request: a contiguous register range of one device and register kind."""

    device_id: int
    kind: str
    address: int
    count: int
    members: tuple[tuple[Point, int], ...]  # (point, offset inside the span)


def build_plan(points: list[Point], max_registers: int = MAX_REGISTERS) -> tuple[Span, ...]:
    """Merge touching or overlapping registers; never across devices, kinds, gaps or the request limit."""
    spans: list[Span] = []
    groups: dict[tuple[int, str], list[Point]] = {}
    for p in points:
        groups.setdefault((p.device_id, p.register_kind), []).append(p)
    for (device, kind), members in sorted(groups.items()):
        members.sort(key=lambda p: p.address)
        run: list[Point] = []
        end = 0

        def flush(device=device, kind=kind, run=run):
            if run:
                start = run[0].address
                spans.append(Span(device, kind, start, end - start, tuple((p, p.address - start) for p in run)))

        for p in members:
            last = p.address + WIDTH[p.data_type]
            if run and p.address <= end and last - run[0].address <= max_registers:
                run.append(p)
                end = max(end, last)
            else:
                flush()
                run.clear()
                run.append(p)
                end = last
        flush()
    return tuple(spans)


def decode_span(span: Span, registers: list[int]):
    """(point, value, quality) for each member; undecodable values are BAD without a number."""
    out = []
    for point, offset in span.members:
        words = registers[offset : offset + WIDTH[point.data_type]]
        value, quality = decode_registers(words, point)
        out.append((point, value, rec.GOOD if quality == "GOOD" else rec.BAD))
    return out


class ModbusAcquirer:
    """Connection owner and round runner for one Modbus source.

    `hub` is the ordered entry to the state owner (Ingress): it receives generations, batches and
    connection loss in the order they happened.
    """

    def __init__(
        self,
        source: Source,
        points: list[Point],
        timing: Timing,
        hub,
        *,
        client_factory: Callable | None = None,
        ticker: Callable = every_second,
        sleep: Callable = asyncio.sleep,
    ):
        self.source, self.points, self.timing, self.hub = source, points, timing, hub
        self.plan = build_plan(points)
        self.client_factory = client_factory or self._default_client
        self.ticker, self.sleep = ticker, sleep
        self.gen = 0
        self.backoff = Backoff(timing)
        self.outcomes: list[str] = []  # recent round outcomes, for health and tests
        self.consecutive_timeouts = 0
        # completed CONNECT attempts that failed since the public connect() last succeeded (终版 §11);
        # nothing else counts: not a failed round, not a cancelled attempt, not a resync.
        self.connect_failures = 0

    def _default_client(self):
        from pymodbus.client import AsyncModbusTcpClient

        t = self.timing
        # reconnect_delay=0: the library must not reconnect on its own; the application decides.
        return AsyncModbusTcpClient(
            self.source.host,
            port=self.source.port,
            timeout=t.modbus_request_timeout,
            retries=t.modbus_retries,
            reconnect_delay=0,
        )

    async def _read(self, client, span: Span):
        read = client.read_holding_registers if span.kind == "holding" else client.read_input_registers
        return await read(span.address, count=span.count, device_id=span.device_id)

    async def poll(self, client, tick: int):
        """One planned round -> (InputBatch, outcome).  Never raises for protocol-level failures."""
        from pymodbus.exceptions import ConnectionException, ModbusIOException

        poll_id = f"{self.source.id}/{self.gen}/{tick}"
        start_wall = time.time()
        items, answered, timed_out, excepted, broken = [], 0, 0, 0, False
        requests = []  # how each request ended, for the communication log
        for k, span in enumerate(self.plan):
            scope = f"{span.kind}/{span.device_id}/{span.address}+{span.count}"
            try:
                response = await self._read(client, span)
            except (ConnectionException, OSError):
                broken = True
                break
            except (ModbusIOException, TimeoutError) as error:
                if asyncio.current_task().cancelling() > 0:  # a cancel mid-request arrives as this error
                    raise asyncio.CancelledError from error
                timed_out += 1
                detail, truncated = clean_detail(str(error))  # the raw text goes no further than this line
                requests.append(rec.RequestResult(scope, "TIMEOUT", None, detail, truncated))
                continue
            wall, mono = time.time(), time.monotonic()  # each response keeps its own receive time
            if response.isError():
                excepted += 1
                requests.append(rec.RequestResult(scope, "EXCEPTION", getattr(response, "exception_code", None)))
                continue
            answered += 1
            requests.append(rec.RequestResult(scope, "OK"))
            for point, value, quality in decode_span(span, response.registers):
                items.append(rec.Item(point.id, value, quality, None, None, f"{poll_id}/{k}", wall, mono))
        end_wall, end_mono = time.time(), time.monotonic()
        if broken:
            outcome = "CONNECTION"
        elif answered:
            outcome = "OK"
        elif excepted:
            outcome = "EXCEPTION"
        else:
            outcome = "TIMEOUT"
        batch = rec.InputBatch(
            self.source.id,
            self.gen,
            rec.KIND_MODBUS_POLL,
            end_wall,
            end_mono,
            tuple(items) if outcome == "OK" else (),
            tick=tick,
            poll_id=poll_id,
            outcome=outcome,
            poll_start_wall=start_wall,
            poll_end_wall=end_wall,
            requests=tuple(requests),
        )
        return batch, outcome

    async def _connect(self, client) -> tuple[bool, str | None, str | None]:
        """(connected, reason code, detail).  The public call's boolean decides; what the library gives away is all
        there is: a False carries no reason (pymodbus swallows it), a timeout of OUR call is told apart from it."""
        limit = self.timing.modbus_connect_timeout
        try:
            connected = bool(await asyncio.wait_for(client.connect(), limit))
        except TimeoutError:  # the application's own limit (before OSError: a timeout is one)
            return False, "APP_CONNECT_TIMEOUT", f"建连调用{limit:g}秒内未返回"
        except OSError as error:
            return False, "ERROR:" + type(error).__name__, repr(error)
        if connected:
            return True, None, None
        return False, "LIBRARY_NO_REASON", "连接失败，库未提供原因"

    async def session(self, client) -> str:
        """Run planned rounds on a live connection; return why the session ended."""
        self.consecutive_timeouts = 0
        async for tick in self.ticker():
            batch, outcome = await self.poll(client, tick)
            self.outcomes.append(outcome)
            del self.outcomes[:-50]
            self.hub.offer(batch)
            if outcome == "CONNECTION":
                return "CONNECTION"
            if outcome == "TIMEOUT":
                self.consecutive_timeouts += 1
                if self.consecutive_timeouts >= self.timing.modbus_reconnect_timeouts:
                    return "TIMEOUTS"
            else:
                self.consecutive_timeouts = 0  # any answer proves the connection is alive
        return "ENDED"

    async def run(self):
        down_reported = False
        while True:
            client = self.client_factory()
            try:
                connected, reason, detail = await self._connect(client)
                if connected:
                    self.connect_failures = 0
                    self.hub.attempt_ok(self.source.id, "CONNECT", time.time(), time.monotonic())
                    self.gen += 1
                    self.hub.begin_generation(self.source.id, self.gen, time.time(), time.monotonic())
                    down_reported = False
                    self.backoff.healthy(time.monotonic())
                    reason = await self.session(client)
                    self.backoff.settle(time.monotonic())
                    self.hub.source_down(self.source.id, self.gen, reason, time.time(), time.monotonic())
                    down_reported = True
                else:
                    self.connect_failures += 1
                    now = (time.time(), time.monotonic())
                    self.hub.attempt_failed(self.source.id, "CONNECT", reason, detail, *now)
                    if not down_reported and self.gen:
                        self.hub.source_down(self.source.id, self.gen, "CONNECT_FAILED", *now)
                        down_reported = True
            finally:
                client.close()
            await self.sleep(self.backoff.next_delay())
