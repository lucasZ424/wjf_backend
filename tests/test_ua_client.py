"""D14-D15: application-managed OPC UA client and subscription against a real local asyncua server.

Why it matters: if the library keeps probing, recreating or reconnecting on its own, the application's
view of validity, generations and backoff is no longer the only one (终版 §4, §11).
"""

import asyncio
import time

import pytest
from asyncua import Client, ua
from asyncua.common.node import Node
from asyncua.common.subscription import Subscription
from protocol_support import FieldUa, PausableProxy

from htsg.config import Point, Timing
from htsg.ua import (
    ItemRevision,
    ManagedClient,
    SubscriptionError,
    negotiate,
    open_subscription,
)

NAMES = ["a", "b", "c"]


@pytest.fixture
async def field():
    f = FieldUa(NAMES)
    await f.start()
    yield f
    await f.stop()


def points_for(field, names=NAMES):
    return [Point(id=n, source="ua", unit="u", node_id=field.node_id(n)) for n in names]


async def test_library_probe_and_recreate_tasks_are_off_while_publish_and_channel_renewal_run(field, monkeypatch):
    reads = []
    original = Node.read_value

    async def counted(self, *args, **kw):
        reads.append(self.nodeid.to_string())
        return await original(self, *args, **kw)

    monkeypatch.setattr(Node, "read_value", counted)
    managed = ManagedClient(field.endpoint, timeout=2, watchdog_intervall=0.2, auto_reconnect=False)
    plain = Client(field.endpoint, timeout=2, watchdog_intervall=0.2, auto_reconnect=False)
    try:
        await plain.connect()
        await asyncio.sleep(1.0)
        probes = len(reads)
        assert probes >= 2, "control: the stock client does probe server_state on its own"
        await plain.disconnect()  # so that only the managed client can be counted below
        reads.clear()
        await managed.connect()
        await asyncio.sleep(1.0)
        assert managed._supervisor_task.done() and managed._stale_watchdog_task.done()
        assert reads == [], "no periodic server_state read by the library"
        renew = getattr(managed, "_renew_channel_task", None)
        assert renew is None or not renew.done(), "secure-channel renewal must keep working"
        values = []
        sub, negotiated = await open_subscription(managed, points_for(field), Timing(), values.append)
        await field.set("a", 1.5)
        deadline = time.monotonic() + 5
        while not values and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert values, "the normal Publish loop still delivers"
        await sub.delete()
    finally:
        await managed.disconnect()
        if plain.uaclient.state.name != "DISCONNECTED":
            await plain.disconnect()


async def test_no_library_recreate_happens_even_when_the_subscription_goes_silent(field, monkeypatch):
    calls = []

    async def forbidden(self):
        calls.append(1)

    monkeypatch.setattr(Subscription, "recreate", forbidden)
    proxy = PausableProxy(field.port)
    await proxy.start()
    client = ManagedClient(f"opc.tcp://127.0.0.1:{proxy.port}/t/", timeout=2, watchdog_intervall=0.2)
    try:
        await client.connect()
        sub, _ = await open_subscription(client, points_for(field), Timing(), lambda result: None)
        proxy.pause()
        await asyncio.sleep(2.5)  # long enough for the library's own stale rule (>= 1 s) to have fired
        proxy.resume()
        await asyncio.sleep(0.3)
        assert calls == []
        await sub.delete_quietly()
    finally:
        await client.disconnect()
        await proxy.stop()


async def test_transport_loss_is_seen_by_the_application_not_pushed_by_the_library(field):
    lost = []

    async def callback(error):
        lost.append(error)

    client = ManagedClient(field.endpoint, timeout=2, watchdog_intervall=0.2)
    client.connection_lost_callback = callback
    results = []
    try:
        await client.connect()
        sub, _ = await open_subscription(client, points_for(field), Timing(), results.append)
        await client.check_connection()  # healthy: returns
        await field.stop()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                await client.check_connection()
            except ConnectionError:
                break
            await asyncio.sleep(0.05)
        else:
            pytest.fail("the application never saw the dropped transport")
        await asyncio.sleep(0.5)
        assert lost == [], "the library's supervisor is off, so it no longer reports the loss itself"
        assert not [x for x in results if x.NotificationMessage.NotificationData and any(
            isinstance(n, ua.StatusChangeNotification) for n in x.NotificationMessage.NotificationData)], (
            "no BadShutdown is pushed into the subscription either"
        )  # fmt: skip
    finally:
        client.disconnect_socket()
        task = getattr(client.uaclient.session, "_publish_task", None)
        if task:
            task.cancel()


# ------------------------------------------------------------------------- D15 negotiation


async def test_subscription_is_created_with_the_specified_explicit_parameters(field):
    client = ManagedClient(field.endpoint, timeout=2)
    client.session_timeout = 60000
    try:
        await client.connect()
        sub, negotiated = await open_subscription(client, points_for(field), Timing(), lambda result: None)
        p = sub.parameters
        assert (p.RequestedPublishingInterval, p.RequestedMaxKeepAliveCount, p.RequestedLifetimeCount) == (1000, 2, 60)
        assert p.RequestedLifetimeCount != 10000, "the library's own default must not be used"
        assert negotiated.requested == {
            "publish_interval": 1, "keepalive_count": 2, "lifetime_count": 60, "queue_size": 10, "sampling_interval": 1,
            "session_timeout": 60,
        }  # fmt: skip
        # what is stored is what the server returned, read back from the creation reply
        assert negotiated.publish_interval == sub.created.RevisedPublishingInterval / 1000
        assert negotiated.keepalive_count == sub.created.RevisedMaxKeepAliveCount
        assert negotiated.lifetime_count == sub.created.RevisedLifetimeCount
        assert negotiated.session_timeout == client.session_timeout / 1000
        assert all(i.sampling_interval > 0 and i.queue_size >= 1 for i in negotiated.items)
        assert len(negotiated.items) == 3 and not negotiated.violations
        await sub.delete()
    finally:
        await client.disconnect()


def fake_created(publish_ms, lifetime, keepalive):
    return ua.CreateSubscriptionResult(
        SubscriptionId=1,
        RevisedPublishingInterval=publish_ms,
        RevisedLifetimeCount=lifetime,
        RevisedMaxKeepAliveCount=keepalive,
    )


def test_revised_values_replace_the_request_and_are_rechecked():
    items = [ItemRevision("a", 0, 1.0, 10)]
    granted = negotiate(Timing(), fake_created(2000, 60, 2), items, 60.0)
    assert granted.publish_interval == 2.0 and granted.requested["publish_interval"] == 1
    assert granted.violations, "a 2 s publish interval makes the normal silence 4 s, beyond the 3 s validity"
    short = negotiate(Timing(), fake_created(1000, 4, 2), items, 60.0)
    assert any("lifetime" in v for v in short.violations)
    assert negotiate(Timing(), fake_created(1000, 60, 2), items, 60.0).violations == []


def test_a_queue_revised_to_one_is_marked_but_not_rejected():
    items = [ItemRevision("a", 0, 1.0, 10), ItemRevision("b", 0, 1.0, 1)]
    granted = negotiate(Timing(), fake_created(1000, 60, 2), items, 60.0)
    assert granted.queue_capability == "OVERWRITE_UNDETECTABLE"
    assert granted.as_dict()["min_queue"] == 1 and granted.violations == []
    assert granted.as_dict()["max_queue"] == 10 and granted.as_dict()["items"] == 2, "the delivery table needs the whole spread, not just the smallest"
    assert (granted.as_dict()["sampling_interval_min"], granted.as_dict()["sampling_interval_max"]) == (1.0, 1.0)
    assert negotiate(Timing(), fake_created(1000, 60, 2), items[:1], 60.0).queue_capability == "OVERFLOW_DETECTABLE"


async def test_a_failed_creation_leaves_no_subscription_on_server_or_client(field):
    client = ManagedClient(field.endpoint, timeout=2)
    missing = [Point(id="x", source="ua", unit="u", node_id=field.node_id("a")),
               Point(id="y", source="ua", unit="u", node_id="ns=2;s=does_not_exist")]  # fmt: skip
    try:
        await client.connect()
        with pytest.raises(SubscriptionError):
            await open_subscription(client, missing, Timing(), lambda result: None)
        assert field.subscription_count == 0, "the server-side subscription must be deleted again"
        assert client.uaclient.session._subscription_callbacks == {}
    finally:
        await client.disconnect()
