"""D36: version snapshot, whole-group push, 100 ms packing and the idle heartbeat (终版 §6, §11).

Why it matters: a client that joins at any moment, or whose messages are packed together, must end up showing
exactly what the owner holds - no group missed, none applied twice, no input shown with another group's result -
and silence must never be mistaken for a sample.
"""

import asyncio
import json
import time

import pytest
from client_model import ClientModel, Rejected
from fault_support import MONO0, WALL0, Accepting, at, fault_config
from live_support import BRIDGES, FULL, PROCESS, UA_NAMES
from owner_support import notify, poll

from htsg import records as r
from htsg.owner import Owner
from htsg.publisher import Publisher


class Clock:
    def __init__(self):
        self.t = MONO0

    def mono(self):
        return self.t

    def wall(self):
        return WALL0 + (self.t - MONO0)


class Rig:
    """An owner whose published groups feed a publisher; time is whatever the test says it is."""

    def __init__(self, config=None, mb="mb", ua="ua"):
        self.clock, self.sink, self.seq, self.mb_id, self.ua_id = Clock(), Accepting(), 0, mb, ua
        self.owner = Owner(
            config or fault_config(ua_only=False), WALL0, MONO0, commit=self.sink,
            publish=lambda group: self.publisher.offer(group), run_id="run",
        )  # fmt: skip
        self.publisher = Publisher(self.owner, clock=(self.clock.wall, self.clock.mono))
        self.sent, self.clients, self.seen = [], [], set()
        self.publisher.subscribers.append(self.sent.append)  # a recorder of what every client is sent
        for source in (mb, ua):
            self.owner.begin_generation(source, 1, WALL0, MONO0)

    def join(self):
        model = ClientModel()
        self.publisher.subscribe(model.apply)
        self.clients.append(model)
        return model

    def step(self, mono, action=None):
        """Move time to `mono`, let the owner act, send what is due - and insist that no state change went
        unannounced: what the groups added up to must equal what the owner holds."""
        self.clock.t = mono
        if action:
            action()
        self.publisher.pump()
        views = self.owner.channel_views()
        assert self.publisher.views == views, "a change of state was not published"
        for view in views.values():
            self.seen |= {view.get("fault"), view.get("reason")}

    def ua(self, mono, items, gen=1):
        self.seq += 1
        wall, mono = at(mono)
        batch = notify(items, source=self.ua_id, gen=gen, wall=wall, mono=mono, seq=self.seq)
        self.step(mono, lambda: self.owner.accept(batch))

    def mb(self, n, values, **kw):
        self.step(100.0 + n, lambda: self.owner.accept(poll(n, values, source=self.mb_id, **kw)))

    def second(self, mono, keepalive=True):
        """One tick; the OPC UA server keeps the subscription alive unless told otherwise."""
        wall, mono = at(mono)
        gen = self.owner.sources[self.ua_id].conn_gen

        def act():
            if keepalive:
                self.owner.accept(r.InputBatch(self.ua_id, gen, r.KIND_UA_NOTIFY, wall, mono))
            self.owner.tick(wall, mono)

        self.step(mono, act)

    def settle(self):
        self.step(self.clock.t + 1)


def story(rig, join_before_every_step):
    """Everything the realtime state can go through, in one run; a client may join before any step."""
    steps = [
        lambda: rig.ua(101, [("ti", 480.0), ("to", 440.0)]),
        lambda: rig.mb(1, {"b1": 0.7, "b2": 0.7}),
        lambda: rig.mb(2, {"b1": 0.8}),  # b2 not answered: POLL_MISSING
        lambda: rig.mb(3, {}, outcome="TIMEOUT"),
        lambda: rig.mb(4, {"b1": 0.9, "b2": 0.9}),
        lambda: rig.mb(5, {"b1": 1.0, "b2": 1.0}),  # the first change between consecutive rounds
        lambda: rig.ua(106, [("ti", 495.0)]),  # above 490: a pending fault
        *[lambda m=m: rig.second(m) for m in range(107, 112)],  # 5 s later: confirmed
        lambda: rig.ua(112, [("ti", 480.0)]),  # back below the reset line: recovered
        *[lambda m=m: rig.second(m, keepalive=False) for m in (113, 114, 115)],  # 3 s of silence: EXPIRED
        lambda: rig.ua(116, [("ti", 481.0)]),  # the same subscription again: new segment, baseline
        *[lambda m=m: rig.second(m, keepalive=False) for m in range(117, 123)],  # 6 s: RESYNC
        lambda: rig.step(124, lambda: rig.owner.begin_generation("ua", 2, *at(124))),
        lambda: rig.ua(125, [("ti", 482.0), ("to", 441.0)], gen=2),
        lambda: setattr(rig.sink, "accept", False),
        lambda: rig.ua(126, [("ti", 483.0)], gen=2),  # the archive refuses: a gap, realtime goes on
        lambda: setattr(rig.sink, "accept", True),
        lambda: rig.second(127),  # the recovery unit is archive-only
        lambda: rig.step(128, lambda: rig.owner.committed(rig.sink.groups[-1])),
        lambda: rig.step(130, lambda: rig.owner.tick(WALL0 + 40.0, 130.0)),  # the wall clock steps 10 s
        lambda: rig.ua(131, [("ti", 9999.0)], gen=2),  # beyond the physical limit: extreme
        lambda: rig.ua(132, [("ti", 485.0, r.BAD, 0x80350000)], gen=2),
        lambda: rig.step(133, lambda: rig.owner.source_down("ua", 2, "CONNECTION", *at(133))),
    ]
    for i, step in enumerate(steps):
        if join_before_every_step or i == 0:
            rig.join()
        step()
    rig.settle()


def test_a_client_joining_at_any_moment_ends_up_with_exactly_the_owner_state():
    rig = Rig()
    story(rig, join_before_every_step=True)
    assert len(rig.clients) >= 30
    expected = rig.owner.channel_views()
    for client in rig.clients:
        assert client.channels == expected and client.seq == rig.owner.pub_seq
    covered = {"PENDING", "CONFIRMED", "NORMAL", "POLL_MISSING", "POLL_TIMEOUT", "QUALITY", "EXTREME"}
    covered |= {"SOURCE_WAITING", "SOURCE_EXPIRED", "SOURCE_RESYNC"}
    assert covered <= rig.seen, f"the story never reached {sorted(covered - rig.seen)}"


def test_every_message_is_whole_ordered_and_without_a_hole_between_snapshot_and_groups():
    rig = Rig()
    story(rig, join_before_every_step=True)
    groups = [g for m in rig.sent if m["type"] == "groups" for g in m["groups"]]
    assert [g["seq"] for g in groups] == list(range(1, rig.owner.pub_seq + 1)), "each group once, in order"
    for m in rig.sent:
        if m["type"] == "groups":
            assert m["first"] == m["groups"][0]["seq"] and m["last"] == m["groups"][-1]["seq"]
            assert [g["seq"] for g in m["groups"]] == list(range(m["first"], m["last"] + 1))


def test_groups_arriving_within_100_ms_share_one_message_and_none_is_merged_away():
    rig = Rig()
    rig.step(100.5)  # what the construction published has left
    sent = len(rig.sent)
    for at_s, ti in ((101.00, 480.0), (101.03, 481.0), (101.06, 482.0)):
        rig.ua(at_s, [("ti", ti), ("to", 440.0)])
    rig.step(101.09)
    assert len(rig.sent) == sent, "the oldest group has not yet waited 100 ms"
    rig.step(101.11)
    (message,) = rig.sent[sent:]
    assert message["type"] == "groups" and len(message["groups"]) == 3
    assert message["last"] - message["first"] == 2
    assert [g["channels"]["ti"]["value"] for g in message["groups"]] == [480.0, 481.0, 482.0], "no final-state merge"
    assert all(g["channels"]["dt"]["value"] == g["channels"]["ti"]["value"] - 440.0 for g in message["groups"])
    rig.ua(101.12, [("ti", 483.0)])
    rig.step(101.21)
    assert len(rig.sent) == sent + 1, "the next window starts with the next group"
    rig.step(101.23)
    assert rig.sent[-1]["first"] == rig.sent[-1]["last"] == message["last"] + 1


def test_a_client_joining_while_groups_wait_gets_them_in_its_snapshot_and_never_again():
    rig = Rig()
    rig.step(100.5)
    rig.ua(101.00, [("ti", 480.0), ("to", 440.0)])
    waiting = rig.owner.pub_seq
    late = rig.join()  # the group above has not left yet
    assert late.seq == waiting and late.channels["ti"]["value"] == 480.0
    rig.ua(101.05, [("ti", 481.0)])
    rig.step(101.30)
    assert late.seq == waiting + 1 and late.channels["ti"]["value"] == 481.0
    assert late.applied == [(waiting + 1, ["dt", "sp", "ti"])]


def test_an_idle_second_sends_a_heartbeat_that_is_not_a_sample():
    rig = Rig()
    client = rig.join()  # lets the start-up groups leave: that is the last thing sent, at t=100
    sent = len(rig.sent)
    before = json.dumps(client.channels, sort_keys=True), list(client.applied)
    rig.step(100.99)
    assert len(rig.sent) == sent, "less than a second idle: nothing is sent"
    rig.step(101.0)
    beat = rig.sent[-1]
    assert len(rig.sent) == sent + 1 and beat["type"] == "heartbeat" and beat["seq"] == rig.owner.pub_seq
    assert not {"channels", "groups"} & set(beat), "a heartbeat carries no sample"
    assert (json.dumps(client.channels, sort_keys=True), list(client.applied)) == before
    rig.step(101.99)
    assert rig.sent[-1] is beat
    rig.step(102.0)
    assert rig.sent[-1] is not beat and rig.sent[-1]["type"] == "heartbeat"


def test_keep_alives_are_not_samples_so_a_quiet_subscription_sends_only_heartbeats():
    rig = Rig()
    rig.ua(101, [("ti", 480.0), ("to", 440.0)])
    rig.step(101.5)
    sent = len(rig.sent)
    for second in range(102, 106):
        rig.second(second)  # the server's keep-alive each second: communication, not data
        rig.step(second + 0.5)
    new = rig.sent[sent:]
    assert new and {m["type"] for m in new} == {"heartbeat"}
    assert rig.sent[-1]["status"]["sources"]["ua"]["last_comm_wall"] == WALL0 + 5.0, "but liveness is reported"


def test_the_client_check_refuses_any_damaged_message_whole_and_changes_nothing():
    rig = Rig()
    rig.step(100.5)
    base = {"type": "snapshot", **rig.owner.snapshot()}
    for at_s in (101.00, 101.02, 101.04):
        rig.ua(at_s, [("ti", 480.0 + at_s)])
    rig.step(101.2)
    good = rig.sent[-1]
    first, middle, last = good["groups"]
    damaged = {
        "wrong last": dict(good, last=last["seq"] + 1),
        "wrong first": dict(good, first=first["seq"] - 1),
        "group missing inside": dict(good, groups=[first, last]),
        "out of order": dict(good, groups=[first, last, middle]),
        "group missing before": dict(good, groups=[middle, last], first=middle["seq"]),
        "empty": dict(good, groups=[], first=0, last=0),
    }
    for what, message in damaged.items():
        client = ClientModel()
        client.apply(base)
        with pytest.raises(Rejected):
            client.apply(message)
        assert (client.seq, client.channels) == (base["pub_seq"], base["channels"]), what
    client = ClientModel()
    client.apply(base)
    client.apply(good)
    assert client.channels == rig.owner.channel_views(), "the undamaged message applies"


def test_a_confirmed_fault_the_full_buffer_refuses_is_still_announced_to_clients():
    rig = Rig(fault_config(ua_only=False, buffer_limit=1))
    rig.join()
    rig.sink.accept = False  # the archive is down: nothing becomes durable, the buffer stays full
    rig.ua(101, [("ti", 495.0), ("to", 440.0)])
    for second in range(102, 107):
        rig.second(second)
    assert rig.owner.fault_status()["buffered"] == 1
    rig.ua(107, [("to", 495.0)])
    for second in range(108, 113):
        rig.second(second)  # `to` is confirmed 5 s later but the buffer has no room
    assert rig.owner.fault_status()["rejected_total"] == 1
    rig.settle()
    view = rig.clients[0].channels["to"]
    assert view["fault"] == "CONFIRMED", "the page shows the fault even though its record could not be kept"
    assert rig.clients[0].status["faults"]["archive_incomplete"] is True


def full_scale_rig():
    rig = Rig(FULL, mb="mb_field", ua="ua_field")
    values = {n: 440.0 if n.startswith("temp_outer") else 480.0 + i * 0.01 if n.startswith("temp_inner") else PROCESS[n]
              for i, n in enumerate(UA_NAMES)}  # fmt: skip
    rig.ua(101, list(values.items()))  # 200 notifications in one batch
    rig.step(101.5)
    return rig, values


def test_the_full_set_of_417_channels_travels_as_valid_json_and_stays_cheap_to_publish():
    rig, _ = full_scale_rig()
    snapshot = rig.owner.snapshot()
    assert len(snapshot["channels"]) == 417
    for message in (*rig.sent, {"type": "snapshot", **snapshot}):
        json.dumps(message, allow_nan=False)  # a browser's JSON.parse refuses NaN and Infinity
    for n in range(2, 5):  # Modbus rounds: 8 bridges and everything computed from them
        rig.mb(n, {b: 0.7 + 0.001 * k + 0.0001 * n for k, b in enumerate(BRIDGES)})
    rig.settle()
    last = [m for m in rig.sent if m["type"] == "groups"][-1]["groups"][-1]
    print(f"channels carried by one Modbus group: {len(last['channels'])} of 417")
    assert 8 <= len(last["channels"]) < 100, "a group carries what it changed, not all 417 channels"
    # what publishing adds to the owner's own work inside every call (the owner needs ~30 ms per round here)
    template = r.PublishGroup(rig.owner.pub_seq, 0, rig.mb_id, 1100.0)
    started = time.perf_counter()
    for _ in range(200):
        rig.publisher.offer(template)
    per_offer = (time.perf_counter() - started) / 200
    print(f"publisher cost per group at 417 channels: {per_offer * 1000:.2f} ms")
    assert per_offer < 0.005, "building views and the change set must stay far below the 100 ms packing window"


async def test_in_real_time_a_group_leaves_within_100_ms_and_idle_means_one_heartbeat_per_second():
    owner = Owner(fault_config(ua_only=False), time.time(), time.monotonic(), publish=lambda g: pub.offer(g), run_id="rt")
    pub = Publisher(owner)
    arrivals = []
    pub.subscribers.append(lambda m: arrivals.append((time.monotonic(), m)))
    task = asyncio.create_task(pub.run())
    try:
        owner.begin_generation("ua", 1, time.time(), time.monotonic())
        offered = {}
        for n in range(1, 6):
            await asyncio.sleep(0.037)
            offered[owner.pub_seq + 1] = time.monotonic()
            owner.accept(notify([("ti", 480.0 + n)], wall=time.time(), mono=time.monotonic(), seq=n))
        await asyncio.sleep(2.3)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    delivered = {g["seq"]: t for t, m in arrivals if m["type"] == "groups" for g in m["groups"]}
    assert set(offered) <= set(delivered)
    assert max(delivered[s] - offered[s] for s in offered) < 0.1 + 0.08, "the 100 ms bound, plus scheduling slack"
    beats = [t for t, m in arrivals if m["type"] == "heartbeat"]
    assert 1 <= len(beats) <= 3 and all(0.9 < b - a < 1.2 for a, b in zip(beats, beats[1:], strict=False))
