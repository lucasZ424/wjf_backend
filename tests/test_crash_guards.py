"""Audit 2026-10-30: what a device, a server or a configuration on site can do must never end the process.

Why it matters: the project's hard rule is a year without a crash.  A core task that ends stops the whole backend and the
supervisor starts it again - against the same device, which answers the same way, so a crash becomes a crash loop.  Each
case here was found ending a core task (or leaking a connection) before the fix.
"""

import asyncio
import time

import pytest
from acquirer_support import FakeResponse, Gate, ScriptedModbus, drain_until, float_registers, stop
from owner_support import Recorder
from protocol_support import bridge_points, fast_ticker
from pydantic import ValidationError
from test_comm_facts import attempt_entries, entries

from htsg.config import Settings, Source, Timing
from htsg.ingress import Ingress
from htsg.modbus import ModbusAcquirer
from htsg.owner import Owner
from htsg.ua import UaAcquirer

SOURCE = Source(id="mb", kind="modbus", enabled=True, host="127.0.0.1", port=1502, interval=1)


def modbus_bench(script, attempts=10_000):
    points = bridge_points()
    config = Settings.model_validate({"sources": [SOURCE.model_dump()], "points": [p.model_dump() for p in points]})
    owner = Owner(config, time.time(), time.monotonic(), commit=Recorder().commit, run_id="run")
    ingress = Ingress(owner, 10_000)
    gate = Gate(attempts)
    acquirer = ModbusAcquirer(
        config.sources[0], points, config.timing, ingress, client_factory=script.factory, ticker=fast_ticker(0.01), sleep=gate
    )
    return owner, ingress, acquirer, gate


# ------------------------------------------------------------------------------------------- Modbus


async def test_a_device_that_answers_fewer_registers_than_asked_is_a_failed_request_not_a_crash():
    """pymodbus checks the frame, not the register count: one register short of a float32 used to end the round with
    struct.error - and the acquirer task with it.  Now the device has answered (no timeout, no reconnect), nothing of the
    request is placed, and the log names the cause."""
    short = ScriptedModbus([True], read=lambda address, count: FakeResponse(float_registers(count)[: count - 1]))
    owner, ingress, acquirer, gate = modbus_bench(short)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: len(acquirer.outcomes) >= 4, why="four short rounds")
        assert not task.done(), "the acquirer is still running"
    finally:
        await stop(task)
    ingress.drain()
    assert set(acquirer.outcomes) == {"EXCEPTION"}, "an answer, unusable: not a timeout, so never a reconnect"
    (entry,) = entries(owner, "POLL_EXCEPTION")
    assert entry.reason == "INCOMPLETE_RESPONSE" and "应答15个寄存器，请求16个" in entry.detail
    s = owner.sources["mb"]
    assert s.state == "VALID" and s.resync_count == 0 and acquirer.gen == 1
    assert not any(owner.points[p.id].good for p in bridge_points()), "no value is taken from a short answer"


async def test_an_error_nobody_foresaw_in_a_round_drops_the_connection_and_acquisition_goes_on():
    rounds = {"n": 0}

    def read(address, count):
        rounds["n"] += 1
        if rounds["n"] == 3:
            raise RuntimeError("something the library was never expected to raise")
        return FakeResponse(float_registers(count))

    owner, ingress, acquirer, gate = modbus_bench(ScriptedModbus([True], read=read), attempts=1)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: gate.blocked, why="the session ended into the backoff wait")
        assert not task.done()
        gate.release.set()
        await drain_until(ingress, lambda: acquirer.gen == 2 and owner.sources["mb"].state == "VALID", why="connected again")
    finally:
        await stop(task)
    ingress.drain()
    resyncs = entries(owner, "RESYNC")
    assert resyncs and resyncs[0].reason == "DOWN:ERROR:RuntimeError"
    assert acquirer.connect_failures == 0, "the connection had been made: not a failed connect"


async def test_an_error_nobody_foresaw_while_connecting_is_a_failed_connect_and_is_tried_again():
    script = ScriptedModbus([RuntimeError("odd"), True])
    owner, ingress, acquirer, gate = modbus_bench(script)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: owner.sources["mb"].state == "VALID", why="the second attempt connects")
    finally:
        await stop(task)
    ingress.drain()
    (entry,) = attempt_entries(owner, "CONNECT")
    assert entry.reason == "ERROR:RuntimeError" and script.calls == 2 and acquirer.connect_failures == 0


# ------------------------------------------------------------------------------------------- OPC UA


class HangingSubscription:
    async def delete_quietly(self):
        await asyncio.sleep(3600)  # a half-open link: no answer ever comes


class RecordingClient:
    def __init__(self):
        self.disconnected = False
        task = asyncio.get_running_loop().create_future()
        self.uaclient = type("U", (), {"session": type("S", (), {"_publish_task": task})()})()

    async def disconnect(self):
        self.disconnected = True

    def disconnect_socket(self):
        pass


async def test_a_subscription_delete_that_never_answers_still_closes_the_connection_and_its_publish_task():
    """The timeout used to leave _teardown before disconnect(): every rebuild on a half-open link left a socket and a
    publish task behind - a slow leak over a year of network trouble."""
    acquirer = UaAcquirer(
        Source(id="ua", kind="opcua", endpoint="opc.tcp://127.0.0.1:4840"), [], Timing(ua_request_timeout=0.05), hub=None
    )
    client = RecordingClient()
    await acquirer._teardown(client, HangingSubscription())
    assert client.disconnected and client.uaclient.session._publish_task.cancelled()


# ------------------------------------------------------------------------------------------- configuration


def ua_config(node_id, enabled=True):
    return {
        "sources": [{"id": "ua", "kind": "opcua", "enabled": enabled, "endpoint": "opc.tcp://10.0.0.1:4840"}],
        "points": [{"id": "t1", "source": "ua", "unit": "°C", "node_id": node_id}],
    }


@pytest.mark.parametrize("node_id", ["ns=2;s=Unit1.T01", "ns=3;i=1001", "i=85"])
def test_a_readable_node_id_passes(node_id):
    Settings.model_validate(ua_config(node_id))


@pytest.mark.parametrize("node_id", ["ns=x;s=T01", "Unit1.T01", "ns=2;i=abc", "ns=2;s=", " ns=2;s=T01", "ns=2;s=T01 "])
@pytest.mark.parametrize("enabled", [True, False], ids=["enabled", "disabled"])
def test_a_node_id_the_client_cannot_use_is_refused_by_the_check_naming_the_point(node_id, enabled):
    """Before: accepted by `htsg check`, then the whole subscription refused on site, again and again."""
    with pytest.raises(ValidationError, match="t1: node_id"):
        Settings.model_validate(ua_config(node_id, enabled))
