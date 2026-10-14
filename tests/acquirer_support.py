"""Helpers for driving an acquirer step by step against an owner, deterministically (no wall-clock races)."""

import asyncio
import struct
import time


async def drain_until(ingress, condition, timeout=30.0, why=""):
    """Hand the owner ONE queued item at a time and look after each, so a state is read the moment it is reached."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out: {why}"
        if not ingress.drain(1):
            await asyncio.sleep(0.002)


async def stop(*tasks):
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class Gate:
    """An acquirer's `sleep`: lets `attempts` backoff waits through, then holds the task at the next one.

    One backoff wait follows every attempt (a failed connect, or a session that ended), so "attempts=5" means exactly
    five attempts were made and the task is parked before the sixth - the count asserted afterwards cannot overshoot.
    `release` lets it go on.
    """

    def __init__(self, attempts):
        self.attempts, self.calls = attempts, 0
        self.blocked = False
        self.release = asyncio.Event()
        self.on_call = None  # optional callable(call number), run when the task arrives at a wait

    async def __call__(self, delay):
        self.calls += 1
        if self.on_call is not None:
            self.on_call(self.calls)
        if self.calls >= self.attempts and not self.release.is_set():
            self.blocked = True
            await self.release.wait()
            self.blocked = False
        await asyncio.sleep(0.001)


# ---------------------------------------------------------------------------------- a scripted Modbus client


class FakeResponse:
    def __init__(self, registers):
        self.registers = registers

    def isError(self):
        return False


def float_registers(count, value=0.5):
    words = []
    for _ in range(count // 2):
        words += list(struct.unpack(">HH", struct.pack(">f", value)))
    return words


class ScriptedModbus:
    """What every client of a source does: `connects` is the result of each connect call in turn (the last one repeats):
    True, False, an exception instance (raised), "hang" (never returns: our own timeout ends it) or "stall" (ditto,
    used to be cancelled in flight).  `read(address, count)` answers or raises for each request."""

    def __init__(self, connects, read=None):
        self.connects, self.calls = list(connects), 0
        self.read = read or (lambda address, count: FakeResponse(float_registers(count)))
        self.watch = None  # callable() -> what to note at every connect call
        self.seen = []

    def factory(self):
        return _Client(self)


class _Client:
    def __init__(self, script):
        self.script = script

    async def connect(self):
        s = self.script
        s.calls += 1
        if s.watch is not None:
            s.seen.append(s.watch())
        step = s.connects[min(s.calls - 1, len(s.connects) - 1)]
        if step in ("hang", "stall"):
            await asyncio.sleep(3600)
        if isinstance(step, BaseException):
            raise step
        return step

    async def read_holding_registers(self, address, count=1, device_id=1):
        return self.script.read(address, count)

    read_input_registers = read_holding_registers

    def close(self):
        pass
