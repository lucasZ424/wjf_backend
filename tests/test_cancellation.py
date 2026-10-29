"""R01 / Q01: one cancel must end an acquirer, whatever its request happens to be waiting for.

pymodbus 3.15.0 turns a cancel that lands while a request waits for its answer into ModbusIOException.  Read as a
timeout round, it made the cancel vanish: the acquirer went on, and everything that waited for it - the runtime's
stop, the end of a test rig - waited with it.  The deciding fact is whether THIS task has an unhandled cancel
request.  A round that merely failed the same way, with nobody cancelling, is still an ordinary timeout round
(终版 §9, §11).
"""

import asyncio
import time

import pytest
from live_support import Rig
from owner_support import small_config
from protocol_support import FieldModbus, bridge_points, fast_ticker
from pymodbus.exceptions import ModbusIOException
from test_archive_router import Disks
from test_simulation import Chain

from htsg import runtime as runtime_module
from htsg.archive import ArchiveStore
from htsg.archive_router import ArchiveRouter
from htsg.config import Settings, Source, Timing
from htsg.modbus import ModbusAcquirer
from htsg.runtime import Runtime

# ------------------------------------------------------------------------------------------ helpers


async def until(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.01)


async def ended(task, seconds=2.0):
    """Whether `task` ended within `seconds` - without cancelling it again, which would hide the answer."""
    done, _ = await asyncio.wait({task}, timeout=seconds)
    return bool(done)


async def reap(task):
    """Whatever the code under test does with a cancel, a test does not leave its task running."""
    for _ in range(50):
        if task.done():
            break
        task.cancel()
        await asyncio.wait({task}, timeout=0.2)
    await asyncio.gather(task, return_exceptions=True)


async def no_wait(delay):
    await asyncio.sleep(0.01)


class HubLog:
    """The ordered entry as the acquirer sees it: every call is kept, in the order it was made."""

    def __init__(self):
        self.calls = []

    def _note(self, name, *detail):
        self.calls.append((name, *detail))
        return True

    def offer(self, batch):
        return self._note("offer", batch.outcome)

    def attempt_ok(self, *args):
        return self._note("attempt_ok")

    def attempt_failed(self, *args):
        return self._note("attempt_failed")

    def begin_generation(self, *args):
        return self._note("begin_generation")

    def source_down(self, source_id, gen, reason, *args):
        return self._note("source_down", reason)

    def outcomes(self):
        return [call[1] for call in self.calls if call[0] == "offer"]


class HeldPeer(FieldModbus):
    """A real Modbus server that takes a request, says so, and withholds the answer until it is released."""

    def __init__(self):
        super().__init__()
        self.arrived, self.release = asyncio.Event(), asyncio.Event()

    async def action(self, *args):
        self.arrived.set()
        await self.release.wait()
        return await super().action(*args)


def watch_clients(acquirer):
    """The clients the acquirer opens, so that a test can ask whether the one in use was closed."""
    made, build = [], acquirer.client_factory

    def factory():
        made.append(build())
        return made[-1]

    acquirer.client_factory = factory
    return made


class CancelLookingClient:
    """Fails a request the way pymodbus 3.15.0 reports a cancelled one - although nobody cancelled anything."""

    connected = True

    async def connect(self):
        return True

    def close(self):
        pass

    async def read_holding_registers(self, *args, **kw):
        raise ModbusIOException("Request cancelled outside library.") from asyncio.CancelledError()


def lone_source():
    return Source(id="mb", kind="modbus", enabled=True, host="127.0.0.1", port=1)


# ------------------------------------------------------------------- the acquirer, over a real client


async def test_one_cancel_while_the_peer_withholds_its_answer_ends_the_acquirer_and_closes_its_client():
    """Q01, the case that was lost: the request is on the wire and unanswered when the cancel comes.  The acquirer
    must end then and there - no timeout round, no reconnect, no going on (终版 §9)."""
    peer, hub = HeldPeer(), HubLog()
    await peer.start()
    acquirer = ModbusAcquirer(peer.source(), bridge_points(), Timing(), hub, ticker=fast_ticker())
    made = watch_clients(acquirer)
    task = asyncio.create_task(acquirer.run())
    try:
        await asyncio.wait_for(peer.arrived.wait(), 5)  # the request is out, its answer is held back
        before = list(hub.calls)
        task.cancel()
        assert await ended(task), "one cancel has to be enough to end the acquirer"
        assert task.cancelled()
        assert not made[-1].connected, "the client is closed on the way out"
        assert hub.calls == before, "a cancelled round is no timeout round: no TIMEOUT batch, no source_down, no retry"
    finally:
        peer.release.set()
        await reap(task)
        await peer.stop()


async def test_a_cancel_when_the_peer_answers_at_once_ends_the_acquirer():
    """Q01 control: with an answering peer the cancel never meets a waiting request, and ends the task as before."""
    peer, hub = FieldModbus(), HubLog()
    await peer.start()
    acquirer = ModbusAcquirer(peer.source(), bridge_points(), Timing(), hub, ticker=fast_ticker(0.2))
    made = watch_clients(acquirer)
    task = asyncio.create_task(acquirer.run())
    try:
        await until(lambda: "OK" in hub.outcomes())
        task.cancel()
        assert await ended(task) and task.cancelled()
        assert not made[-1].connected
    finally:
        await reap(task)
        await peer.stop()


async def test_a_cancel_between_two_rounds_ends_the_acquirer():
    """Q01 control: the cancel arrives while the acquirer sleeps until its next planned tick."""
    peer, hub = FieldModbus(), HubLog()
    await peer.start()

    def one_tick_then_sleep():
        async def ticks():
            yield 0
            await asyncio.sleep(60)
            yield 1

        return ticks

    acquirer = ModbusAcquirer(peer.source(), bridge_points(), Timing(), hub, ticker=one_tick_then_sleep())
    made = watch_clients(acquirer)
    task = asyncio.create_task(acquirer.run())
    try:
        await until(lambda: "OK" in hub.outcomes())
        task.cancel()
        assert await ended(task) and task.cancelled()
        assert not made[-1].connected
    finally:
        await reap(task)
        await peer.stop()


async def test_a_real_request_timeout_without_a_cancel_is_still_a_timeout_round_and_the_acquirer_goes_on():
    """Q01 control, 终版 §9, §11: an unanswered request is an ordinary timeout round; telling a cancel apart from it
    must not turn timeouts into the end of the task.  The peer never answers: a late answer would be tagged by the
    pymodbus server with the transaction id of the newest request and then count as that request's answer."""
    peer, hub = HeldPeer(), HubLog()
    await peer.start()
    timing = Timing(modbus_request_timeout=0.15, modbus_connect_timeout=0.3)
    acquirer = ModbusAcquirer(peer.source(), bridge_points(), timing, hub, ticker=fast_ticker(), sleep=no_wait)
    task = asyncio.create_task(acquirer.run())
    try:
        await until(lambda: hub.outcomes().count("TIMEOUT") >= 2)
        assert not task.done(), "the acquirer keeps running through timeout rounds"
    finally:
        peer.release.set()
        await reap(task)
        await peer.stop()


# ----------------------------------------------------------------------------- the counter-example


async def test_poll_records_a_timeout_round_for_an_error_that_only_looks_like_a_cancel():
    """Q01 counter-example: ModbusIOException(...) from CancelledError, but this task is not being cancelled.  Whether
    a cancel is pending is the criterion; the cause chain alone is not (整改方案 R01)."""
    acquirer = ModbusAcquirer(lone_source(), bridge_points(), Timing(), HubLog())
    batch, outcome = await acquirer.poll(CancelLookingClient(), 7)
    assert outcome == "TIMEOUT" and [request.outcome for request in batch.requests] == ["TIMEOUT"]


async def test_an_error_that_only_looks_like_a_cancel_does_not_end_the_acquirer():
    """Q01 counter-example at task level: the rounds are timeout rounds, three in a row reconnect, the task lives."""
    hub = HubLog()
    acquirer = ModbusAcquirer(
        lone_source(), bridge_points(), Timing(), hub,
        client_factory=CancelLookingClient, ticker=fast_ticker(0.005), sleep=no_wait,
    )  # fmt: skip
    task = asyncio.create_task(acquirer.run())
    try:
        await until(lambda: hub.outcomes().count("TIMEOUT") >= 4)
        assert not task.done()
        assert ("source_down", "TIMEOUTS") in hub.calls
    finally:
        await reap(task)


# --------------------------------------------------------------------------------------- the runtime


def idle_runtime(monkeypatch, tmp_path):
    """A runtime over archives that cannot be reached: its tasks run, nothing is written."""
    monkeypatch.setattr(runtime_module, "STOP_WRITE_WAIT", 0.5)
    config = small_config(
        sources=[{"id": "mb", "kind": "modbus", "enabled": True, "host": "127.0.0.1", "port": 1}, {"id": "ua", "kind": "opcua"}]
    )  # fmt: skip
    dead = "host=127.0.0.1 port=1 dbname=htsg user=nobody connect_timeout=1"
    return Runtime(config, ArchiveRouter(config, ArchiveStore(dead), ArchiveStore(dead), disk_usage=Disks()), number_file=tmp_path / "bucket_number.json")


async def test_a_core_task_that_ends_cancelled_while_the_runtime_runs_is_a_failure(monkeypatch, tmp_path):
    """Q01 / 整改方案 R01: a task cancelled from outside, with nobody having asked the runtime to stop, is lost input;
    the supervisor must see it as the failure it is instead of the process going on without that source."""
    runtime = idle_runtime(monkeypatch, tmp_path)
    await runtime.start()
    try:
        next(task for task in runtime.tasks if task.get_name() == "acquire mb").cancel()
        await asyncio.wait_for(runtime.failed.wait(), 3)
        assert runtime.health["fatal"] == "acquire mb: CANCELLED"
    finally:
        await runtime.stop()


async def test_the_cancels_of_a_normal_stop_are_no_failure(monkeypatch, tmp_path):
    """Q01 control: stopping cancels every task on purpose; none of it is reported as fatal."""
    runtime = idle_runtime(monkeypatch, tmp_path)
    await runtime.start()
    await runtime.stop()
    assert runtime.health["fatal"] is None and not runtime.failed.is_set()


# ------------------------------------------------------------------------------ the test rigs' own exits


async def leave(context, release, seconds=10.0):
    """`context.__aexit__` under an upper bound: an exit that does not end is the failure.  `release` lets the held
    answers go and the tasks are cancelled again until they are gone, so the failure is reported, not a hung run."""
    closing = asyncio.ensure_future(context.__aexit__(None, None, None))
    done, _ = await asyncio.wait({closing}, timeout=seconds)
    if closing in done:
        closing.result()
        return
    release()
    for _ in range(50):
        for task in context.tasks:
            task.cancel()
        done, _ = await asyncio.wait({closing}, timeout=0.2)
        if done:
            break
    pytest.fail(f"{type(context).__name__}.__aexit__ did not end within {seconds:g} s")


async def test_the_full_scale_rig_ends_ten_times_while_its_modbus_peer_answers_slowly():
    """Q01: Rig.__aexit__ cancels and waits without a limit.  With a peer that answers in 0.6 s the cancel meets a
    waiting request every time, so the exit used to hang."""
    for _ in range(10):
        rig = Rig()
        rig.mb.delay = 0.6
        await rig.__aenter__()
        try:
            await rig.until(lambda: len(rig.mb.requests) > 0, why="a slow request is on the wire")
        finally:
            await leave(rig, lambda: setattr(rig.mb, "delay", 0))


async def test_the_simulation_chain_ends_ten_times_while_its_modbus_peer_answers_slowly():
    """Q01: the same for Chain.__aexit__ of the simulation tests."""
    peer = FieldModbus()
    peer.delay = 0.6
    await peer.start()
    config = Settings.model_validate(
        {
            "sources": [{"id": "mb", "kind": "modbus", "enabled": True, "host": "127.0.0.1", "port": peer.port}],
            "points": [p.model_dump() for p in bridge_points()],
        }
    )
    try:
        for _ in range(10):
            seen = len(peer.requests)
            chain = Chain(config)
            await chain.__aenter__()
            try:
                await until(lambda: len(peer.requests) > seen)
            finally:
                await leave(chain, lambda: setattr(peer, "delay", 0))
    finally:
        peer.delay = 0
        await peer.stop()
