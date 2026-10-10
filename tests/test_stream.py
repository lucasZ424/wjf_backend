"""D37: a client that cannot keep up is cut off at once and starts again from a snapshot (终版 §6, §11).

Why it matters: increments applied after a hole would put wrong numbers on a screen that looks healthy, so a lost
increment ends the connection; and one slow browser must never slow the other clients or the archive.
"""

import asyncio
import time
import types
from contextlib import asynccontextmanager

import pytest
from client_model import ClientModel
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fault_support import fault_config
from owner_support import notify
from test_publisher import Rig

from htsg import stream_api
from htsg.config import Settings
from htsg.owner import Owner
from htsg.publisher import Publisher
from htsg.stream_api import build_stream_router, serve


def config(limits=None, timing=None):
    payload = fault_config(ua_only=False).model_dump(mode="json")
    payload["limits"].update(limits or {})
    payload["timing"].update(timing or {})
    return Settings.model_validate(payload)


def message_at(rig, mono, ti):
    """One input change that leaves as a message of its own (the next one is more than 100 ms away)."""
    rig.ua(mono, [("ti", ti)])
    rig.step(mono + 0.2)


async def let_it_run():
    for _ in range(10):
        await asyncio.sleep(0)


async def test_a_client_whose_outbox_overflows_is_cut_off_at_once_and_nothing_queued_is_sent_later():
    rig = Rig(config({"ws_queue": 2}))
    slow = rig.publisher.connect()  # the snapshot takes one of the two places
    message_at(rig, 101, 470.0)
    assert not slow.lost and len(slow.queue) == 2
    message_at(rig, 102, 471.0)  # there is no place for this one: a hole
    assert slow.lost and not slow.queue, "stopped at once; the older messages are not sent after a hole"
    assert await slow.next() is None
    assert slow.push not in rig.publisher.subscribers, "no more is even offered to it"


async def test_the_other_clients_and_the_archive_do_not_notice_a_client_that_was_cut_off():
    rig = Rig(config({"ws_queue": 2}))
    slow = rig.publisher.connect()
    fast = ClientModel()
    rig.publisher.subscribe(fast.apply)
    for i in range(6):
        message_at(rig, 101 + i, 470.0 + i)
    assert slow.lost
    assert fast.channels == rig.owner.channel_views() and fast.seq == rig.owner.pub_seq
    assert [g.order for g in rig.sink.groups] == list(range(1, rig.owner.order + 1)), "every group was archived"


async def test_a_cut_off_client_starts_from_a_full_snapshot_and_continues_without_a_hole():
    rig = Rig(config({"ws_queue": 2}))
    for attempt in range(2):  # waiting for the snapshot is no protection: not reading overflows again
        waiting = rig.publisher.connect()
        for i in range(3):
            message_at(rig, 101 + attempt * 10 + i, 470.0 + attempt * 10 + i)
        assert waiting.lost
    again = rig.publisher.connect()
    snapshot = await again.next()
    assert snapshot["type"] == "snapshot" and snapshot["pub_seq"] == rig.owner.pub_seq
    assert snapshot["channels"] == rig.owner.channel_views()
    model = ClientModel()
    model.apply(snapshot)
    message_at(rig, 130, 480.0)
    while again.queue:
        model.apply(await again.next())
    assert model.seq == rig.owner.pub_seq and model.channels == rig.owner.channel_views()


def test_the_client_limit_refuses_the_next_connection():
    rig = Rig(config({"max_clients": 1}))
    rig.publisher.subscribers.clear()  # the rig's own recorder is not a client
    rig.publisher.connect()
    with pytest.raises(RuntimeError, match="CLIENT_LIMIT"):
        rig.publisher.connect()


# ----------------------------------------------------------------------- the endpoint


class FakeWs:
    def __init__(self, credential="ok", origin=None):
        self.headers = {"host": "hmi.local", **({"origin": origin} if origin else {})}
        self.url = types.SimpleNamespace(scheme="ws")
        self.credential, self.sent, self.closed, self.accepted = credential, [], None, False
        self.gate = None  # while set and closed, a send does not complete: a client that does not read

    async def accept(self):
        self.accepted = True

    async def receive_json(self):
        return {"credential": self.credential}

    async def send_json(self, message):
        if self.gate is not None:
            await self.gate.wait()
        self.sent.append(message)

    async def close(self, code):
        self.closed = code


class Accounts:
    def __init__(self):
        self.active = True

    async def verify(self, token):
        return {"username": "viewer"} if token == "ok" else None

    def ok(self, identity):
        return self.active


def start(rig, ws, accounts=None):
    accounts = accounts or Accounts()
    return asyncio.create_task(serve(ws, rig.publisher, accounts.verify, accounts.ok, rig.owner.config)), accounts


async def test_the_endpoint_sends_the_snapshot_first_and_then_every_group_in_order():
    rig = Rig()
    ws = FakeWs()
    task, _ = start(rig, ws)
    await let_it_run()
    message_at(rig, 101, 470.0)
    message_at(rig, 102, 471.0)
    await let_it_run()
    assert [m["type"] for m in ws.sent][0] == "snapshot"
    model = ClientModel()
    for message in ws.sent:
        model.apply(message)
    assert model.channels == rig.owner.channel_views() and model.seq == rig.owner.pub_seq
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(rig.publisher.subscribers) == 1, "a gone client no longer receives anything (the rig's recorder stays)"


async def test_a_send_that_takes_longer_than_the_send_timeout_closes_the_connection():
    rig = Rig(config(timing={"client_send_timeout": 0.2}))
    ws = FakeWs()
    ws.gate = asyncio.Event()  # never opened: the browser is not reading
    started = time.monotonic()
    task, _ = start(rig, ws)
    await asyncio.wait_for(task, 3)
    assert ws.closed == stream_api.TRY_AGAIN and ws.sent == []
    assert 0.2 <= time.monotonic() - started < 1.0
    assert len(rig.publisher.subscribers) == 1


async def test_a_client_that_stops_reading_is_closed_when_the_next_increment_finds_no_place():
    rig = Rig(config({"ws_queue": 2}))
    ws = FakeWs()
    ws.gate = asyncio.Event()  # the snapshot is on its way and never arrives
    task, _ = start(rig, ws)
    await let_it_run()
    for i in range(3):  # two wait in the outbox, the third has no place
        message_at(rig, 101 + i, 470.0 + i)
    ws.gate.set()
    await asyncio.wait_for(task, 3)
    assert ws.closed == stream_api.TRY_AGAIN
    assert [m["type"] for m in ws.sent] == ["snapshot"], "only what was already being sent; nothing after the hole"


async def test_credentials_origin_and_a_deactivated_account_are_checked():
    rig = Rig()
    wrong = FakeWs(credential="no")
    task, _ = start(rig, wrong)
    await asyncio.wait_for(task, 2)
    assert wrong.accepted and wrong.closed == stream_api.POLICY and wrong.sent == []
    foreign = FakeWs(origin="http://elsewhere.example")
    task, _ = start(rig, foreign)
    await asyncio.wait_for(task, 2)
    assert not foreign.accepted and foreign.closed == stream_api.POLICY
    ws = FakeWs(origin="http://hmi.local")  # the page was served by the same host
    task, accounts = start(rig, ws)
    await let_it_run()
    assert ws.sent and not task.done()
    accounts.active = False
    sent = len(ws.sent)
    rig.step(rig.clock.t + 1.0)  # the heartbeat reaches the endpoint, which asks about the account
    await asyncio.wait_for(task, 2)
    assert ws.closed == stream_api.POLICY and len(ws.sent) == sent, "nothing more is sent to it"


async def test_a_connection_that_never_presents_a_credential_is_closed(monkeypatch):
    monkeypatch.setattr(stream_api, "AUTH_WAIT", 0.1)
    rig = Rig()

    class Silent(FakeWs):
        async def receive_json(self):
            await asyncio.sleep(10)

    ws = Silent()
    task, _ = start(rig, ws)
    await asyncio.wait_for(task, 2)
    assert ws.closed == stream_api.TRY_AGAIN and not rig.publisher.subscribers[1:]


def test_over_a_real_websocket_the_snapshot_comes_first_and_a_reconnect_starts_from_a_newer_one():
    cfg = config()
    holder = {}
    owner = Owner(cfg, time.time(), time.monotonic(), publish=lambda g: holder["pub"].offer(g), run_id="ws")
    holder["pub"] = publisher = Publisher(owner)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(publisher.run())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def verify(token):
        return {"username": "viewer"} if token == "ok" else None

    app = FastAPI(lifespan=lifespan)
    app.include_router(build_stream_router(publisher, verify, lambda identity: True, cfg))
    with TestClient(app) as client:
        on_loop = client.portal.call  # the owner belongs to the event loop thread, like in production
        on_loop(lambda: owner.begin_generation("ua", 1, time.time(), time.monotonic()))
        seqs = []
        for round_ in range(2):
            with client.websocket_connect("/api/stream") as ws:
                ws.send_json({"credential": "ok"})
                model = ClientModel()
                first = ws.receive_json()
                assert first["type"] == "snapshot"
                model.apply(first)
                seqs.append(first["pub_seq"])
                batch = notify([("ti", 470.0 + round_)], wall=time.time(), mono=time.monotonic(), seq=round_ + 1)
                on_loop(lambda b=batch: owner.accept(b))
                while model.seq < owner.pub_seq:  # a heartbeat may come first
                    model.apply(ws.receive_json())
                assert model.channels == on_loop(owner.channel_views)
        assert seqs[1] > seqs[0], "the second connection took a newer snapshot"
