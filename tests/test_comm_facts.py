"""D44c: what the two protocols and the owner really tell the communication log (终版 §15.1, §11).

Why it matters: an operator reads this log to learn WHY a source is quiet and WHEN it came back.  Facts that sound alike
must stay apart (first communication, recovery after an interruption, clearing an alarm; a connect that failed, a
subscription that failed, a request that timed out), each failure must carry only what the application really knows, and
the counters that stand beside the log (`connect_failures`) must follow the protocol call and nothing else.

Dependence: 方案 D44c; 审核 §8.1 S4、§8.3 P1/P2、§8.4 R1、§8.5 "实施时按此理解"; 终版 §11 约束第3条、§15.1.
"""

import asyncio
import time

import pytest
from acquirer_support import FakeResponse, Gate, ScriptedModbus, drain_until, stop
from owner_support import Recorder
from protocol_support import FieldUa, fast_ticker
from pymodbus.exceptions import ConnectionException, ModbusIOException
from pymodbus.pdu import ExceptionResponse
from test_resync_health import Bench, at

from htsg import records as r
from htsg.config import Point, Settings, Timing
from htsg.ingress import Ingress
from htsg.modbus import ModbusAcquirer
from htsg.owner import Owner
from htsg.ua import UaAcquirer


def events(owner):
    """The owner's own facts and the failed rounds, in order; attempt failures are looked at through attempt_entries."""
    return [e.event for e in owner.commlog.entries.values() if e.kind != "ATTEMPT"]


def entries(owner, event):
    return [e for e in owner.commlog.entries.values() if e.event == event]


# ============================================================ the owner's own facts (deterministic, no protocol)


def test_first_communication_recovery_and_alarm_clearing_are_three_separate_facts():
    b = Bench("opcua")
    b.owner.begin_generation("ua", 1, *at(101.0))
    b.owner.begin_waiting("ua", 1, 101.0)
    assert events(b.owner) == ["NEW_GENERATION"], "connected and subscribed is not communicating"

    b.respond(1, 102.0)  # the first message is only a keep-alive: it still proves the link
    assert events(b.owner) == ["NEW_GENERATION", "FIRST_COMM"]
    assert entries(b.owner, "FIRST_COMM")[0].as_dict()["last_comm_time"] is None

    b.down(1, 103.0)
    (resync,) = entries(b.owner, "RESYNC")
    d = resync.as_dict()
    assert d["reason"] == "DOWN:CONNECTION" and d["conn_gen"] == 1
    assert d["last_comm_time"] == at(102.0)[0] and d["found_time"] == at(103.0)[0], "last at T0, noticed at T1"
    assert d["silent_seconds"] == pytest.approx(1.0)

    b.generation(2, 105.0)
    assert "COMM_RECOVERED" not in events(b.owner), "a new generation is not a recovery"
    b.respond(2, 107.0)  # no alarm was ever raised: the recovery is still recorded
    (recovered,) = entries(b.owner, "COMM_RECOVERED")
    assert recovered.as_dict()["silent_seconds"] == pytest.approx(5.0), "from the last valid response (102) to this one (107)"
    assert recovered.conn_gen == 2 and "COMM_ALARM" not in events(b.owner)

    b.tick(110.0)  # 3 s quiet: the data expire
    b.tick(113.0)  # 6 s quiet: the alarm and the rebuild request
    assert events(b.owner)[-3:] == ["DATA_EXPIRED", "COMM_ALARM", "RESYNC"]
    b.generation(3, 114.0)
    b.respond(3, 115.0)
    assert events(b.owner)[-3:] == ["NEW_GENERATION", "COMM_RECOVERED", "COMM_ALARM_CLEARED"]
    again = entries(b.owner, "COMM_RECOVERED")[-1]
    assert again.as_dict()["silent_seconds"] == pytest.approx(8.0)
    assert len(entries(b.owner, "COMM_ALARM_CLEARED")) == 1, "the alarm cleared once; the recovery is a different line"


@pytest.mark.parametrize(("protocol", "how"), [("opcua", "bad_value"), ("modbus", "exception"), ("modbus", "answer")])
def test_the_first_message_of_any_valid_kind_is_the_first_communication(protocol, how):
    """A Bad value inside a normal publish, or a device's exception answer, still shows the other end is alive."""
    b = Bench(protocol)
    b.generation(1, 101.0)
    b.respond(1, 102.0, how)
    assert events(b.owner) == ["NEW_GENERATION", "FIRST_COMM"]


def test_one_timed_out_round_is_not_an_interruption_so_the_next_answer_is_not_a_recovery():
    b = Bench("modbus")
    b.generation(1, 101.0)
    b.respond(1, 102.0)
    b.timeout_round(1, 103.0)
    b.respond(1, 104.0)
    assert events(b.owner) == ["NEW_GENERATION", "FIRST_COMM"]
    for second in (105.0, 106.0, 107.0):  # three in a row do reach the reconnect rule
        b.timeout_round(1, second)
    b.down(1, 107.5, "TIMEOUTS")
    b.generation(2, 108.0)
    b.respond(2, 109.0)
    assert events(b.owner)[-3:] == ["RESYNC", "NEW_GENERATION", "COMM_RECOVERED"]


def test_each_state_change_is_one_line_however_often_it_is_reported_or_read():
    b = Bench("opcua")
    b.generation(1, 101.0)
    b.respond(1, 102.0)
    b.down(1, 103.0)
    b.down(1, 104.0)  # the same disconnect again
    b.owner.lost("ua", 1, "SEQUENCE_GAP", *at(105.0))  # already in RESYNC
    b.owner.report_loss("ua", 1, "INGRESS_FULL", *at(106.0), count=3)
    b.owner.report_loss("ua", 0, "INGRESS_FULL", *at(106.5), count=9)  # an older generation
    for _ in range(3):  # reading the state is not an event
        b.owner.status(), b.owner.channel_views(), b.owner.snapshot(), b.owner.commlog.snapshot()
    assert events(b.owner) == ["NEW_GENERATION", "FIRST_COMM", "RESYNC", "APP_LOSS"]
    assert entries(b.owner, "APP_LOSS")[0].count == 3, "the entry counts the batches lost"
    assert entries(b.owner, "RESYNC")[0].reason == "DOWN:CONNECTION"


def test_an_expiry_is_logged_once_and_a_source_that_never_answered_says_so():
    b = Bench("opcua")
    b.generation(1, 101.0)
    for m in range(102, 112):
        b.tick(float(m))  # never an answer: the alarm at 6 s, the rebuild request at 6 s
    assert events(b.owner) == ["NEW_GENERATION", "COMM_ALARM", "RESYNC"], "no data ever, so nothing 'expired'"
    d = entries(b.owner, "COMM_ALARM")[0].as_dict()
    assert d["reason"] == "NEVER_RESPONDED" and d["last_comm_time"] is None and d["silent_seconds"] is None


# ----------------------------------------------------------------------- failed rounds, per request


def mb_round(b, tick, mono, *requests, gen=1):
    wall, mono = at(mono)
    outcome = (
        "OK" if any(q.outcome == "OK" for q in requests)
        else "EXCEPTION" if any(q.outcome == "EXCEPTION" for q in requests)
        else "TIMEOUT"
    )  # fmt: skip
    items = (r.Item("b1", 0.5, r.GOOD, None, None, "x"), r.Item("b2", 0.5, r.GOOD, None, None, "x")) if outcome == "OK" else ()
    batch = r.InputBatch(
        "mb", gen, r.KIND_MODBUS_POLL, wall, mono, items, tick=tick, poll_id=f"p{tick}", outcome=outcome, requests=tuple(requests)
    )  # fmt: skip
    return b.owner.accept(batch)


A, B = "holding/1/0+4", "holding/1/10+2"


def test_failed_requests_are_kept_per_request_and_a_request_that_answers_ends_only_its_own():
    b = Bench("modbus")
    b.generation(1, 101.0)
    for tick in (1, 2):
        mb_round(b, tick, 101.0 + tick, r.RequestResult(A, "OK"), r.RequestResult(B, "TIMEOUT", None, "no response"))
    (failing,) = [e for e in b.owner.commlog.entries.values() if e.event == "POLL_TIMEOUT"]
    assert (failing.scope, failing.count, failing.state, failing.conn_gen) == (B, 2, "ACTIVE", 1)
    assert [e for e in b.owner.commlog.entries.values() if e.scope == A] == [], "the request that answered logs nothing"
    mb_round(b, 3, 104.0, r.RequestResult(A, "OK"), r.RequestResult(B, "TIMEOUT"))
    assert failing.count == 3, "A answering does not end what still fails in B"
    mb_round(b, 4, 105.0, r.RequestResult(A, "OK"), r.RequestResult(B, "OK"))
    assert (failing.state, failing.end_reason, failing.ended_wall) == ("ENDED", "SUCCESS", at(105.0)[0])
    assert failing.ended_wall > failing.last_wall, "the end is its own time, apart from the last failure"


def test_exception_answers_keep_their_protocol_code_and_exception_ok_exception_is_two_entries():
    b = Bench("modbus")
    b.generation(1, 101.0)
    mb_round(b, 1, 102.0, r.RequestResult(A, "EXCEPTION", 2))
    mb_round(b, 2, 103.0, r.RequestResult(A, "EXCEPTION", 2))
    assert b.s.last_comm_mono == 103.0, "an exception answer is valid communication ..."
    mb_round(b, 3, 104.0, r.RequestResult(A, "OK"))
    mb_round(b, 4, 105.0, r.RequestResult(A, "EXCEPTION", 2))
    mb_round(b, 5, 106.0, r.RequestResult(A, "EXCEPTION", 4))  # a different code is a different failure
    kinds = [(e.event, e.reason, e.count, e.state, e.end_reason) for e in b.owner.commlog.entries.values() if e.scope == A]
    assert kinds == [
        ("POLL_EXCEPTION", "EXCEPTION_CODE_2", 2, "ENDED", "SUCCESS"),
        ("POLL_EXCEPTION", "EXCEPTION_CODE_2", 1, "ENDED", "REASON_CHANGED"),
        ("POLL_EXCEPTION", "EXCEPTION_CODE_4", 1, "ACTIVE", None),
    ], "... but it is not a successful round, and it is never presented as a network disconnect"
    assert not any(e.event == "RESYNC" for e in b.owner.commlog.entries.values())


def test_a_failure_stretch_survives_the_failure_signals_it_causes_and_ends_with_the_new_generation():
    b = Bench("modbus")
    b.generation(1, 101.0)
    b.respond(1, 102.0)
    for tick in range(10, 13):
        mb_round(b, tick, 100.0 + tick, r.RequestResult(A, "TIMEOUT"))
    (stretch,) = [e for e in b.owner.commlog.entries.values() if e.event == "POLL_TIMEOUT"]
    b.down(1, 112.5, "TIMEOUTS")
    assert stretch.state == "ACTIVE" and stretch.count == 3, "the resync is the same trouble speaking, not a cut"
    b.generation(2, 114.0)
    assert (stretch.state, stretch.end_reason, stretch.ended_wall) == ("ENDED", "NEW_GENERATION", at(114.0)[0])
    assert events(b.owner)[-3:] == ["POLL_TIMEOUT", "RESYNC", "NEW_GENERATION"], "ending is not a recovery line"
    mb_round(b, 20, 120.0, r.RequestResult(A, "TIMEOUT"), gen=2)
    assert len([e for e in b.owner.commlog.entries.values() if e.event == "POLL_TIMEOUT"]) == 2, "no continuation across generations"


def test_a_request_detail_is_cleaned_before_it_is_logged():
    b = Bench("modbus")
    b.generation(1, 101.0)
    mb_round(b, 1, 102.0, r.RequestResult(A, "TIMEOUT", None, "no answer from modbus://plc:s3cret@10.0.0.9 password=hunter2"))
    (entry,) = [e for e in b.owner.commlog.entries.values() if e.event == "POLL_TIMEOUT"]
    assert "s3cret" not in entry.detail and "hunter2" not in entry.detail and "no answer from" in entry.detail


# ================================================================ Modbus acquirer (scripted client, stepped)


def modbus_bench(script, *, addresses=(0, 2), timing=None):
    timing = timing or Timing()
    points = [
        {"id": f"p{a}", "source": "mb", "unit": "mV/V", "address": a, "data_type": "float32", "extreme_low": -30, "extreme_high": 30}
        for a in addresses
    ]
    config = Settings.model_validate(
        {
            "sources": [{"id": "mb", "kind": "modbus", "enabled": True, "host": "127.0.0.1", "port": 15020}],
            "points": points,
            "timing": timing.model_dump(),
        }
    )
    owner = Owner(config, time.time(), time.monotonic(), commit=Recorder().commit, run_id="run")
    ingress = Ingress(owner, 10_000)

    def build(attempts):
        gate = Gate(attempts)
        acquirer = ModbusAcquirer(
            config.sources[0], [Point(**p) for p in points], timing, ingress,
            client_factory=script.factory, ticker=fast_ticker(0.001), sleep=gate,
        )  # fmt: skip
        script.watch = lambda: acquirer.connect_failures
        return acquirer, gate

    return owner, ingress, build


async def run_attempts(owner, ingress, build, attempts):
    acquirer, gate = build(attempts)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: gate.blocked, why=f"{attempts} attempts made")
    finally:
        await stop(task)
    ingress.drain()
    return acquirer


def attempt_entries(owner, phase):
    return [e for e in owner.commlog.entries.values() if e.phase == phase]


async def test_every_failed_connect_counts_once_with_its_own_reason_and_a_success_clears_the_count():
    def dropped(address, count):
        raise ConnectionException("closed")

    script = ScriptedModbus([False, ConnectionRefusedError("refused by 10.0.0.9"), "hang", True], dropped)
    owner, ingress, build = modbus_bench(script, timing=Timing(modbus_connect_timeout=0.05))
    acquirer = await run_attempts(owner, ingress, build, 4)
    assert script.seen == [0, 1, 2, 3], "False, an exception and our own timeout each count one"
    assert acquirer.connect_failures == 0, "the public connect() succeeding clears it at once"
    reasons = [(e.reason, e.count, e.end_reason, e.conn_gen) for e in attempt_entries(owner, "CONNECT")]
    assert reasons == [
        ("LIBRARY_NO_REASON", 1, "REASON_CHANGED", None),
        ("ERROR:ConnectionRefusedError", 1, "REASON_CHANGED", None),
        ("APP_CONNECT_TIMEOUT", 1, "CONNECT_OK", None),
    ]
    details = [e.detail for e in attempt_entries(owner, "CONNECT")]
    assert details[0] == "连接失败，库未提供原因", "the library gives no reason: say exactly that, guess nothing"
    assert "refused by 10.0.0.9" in details[1]
    assert details[2] == "建连调用0.05秒内未返回", "our own limit, worded as what is known: the call did not return in time"


async def test_a_first_start_that_never_connects_counts_failures_but_no_resync():
    owner, ingress, build = modbus_bench(ScriptedModbus([False]))
    acquirer = await run_attempts(owner, ingress, build, 5)
    s = owner.sources["mb"]
    assert acquirer.connect_failures == 5 and s.resync_count == 0 and s.conn_gen == 0
    (entry,) = attempt_entries(owner, "CONNECT")
    assert (entry.count, entry.state, entry.conn_gen) == (5, "ACTIVE", None), "reported though no generation ever existed"
    assert events(owner) == [], "no resync, no generation: only the failed attempts say anything"


async def test_after_a_disconnect_five_failed_connects_count_five_while_the_resync_count_stays_one():
    served = []

    def read(address, count):
        served.append(1)
        if len(served) > 2:
            raise ConnectionException("link dropped")
        return FakeResponse([0] * count)

    owner, ingress, build = modbus_bench(ScriptedModbus([True, False], read))
    acquirer = await run_attempts(owner, ingress, build, 6)
    s = owner.sources["mb"]
    assert acquirer.connect_failures == 5 and s.resync_count == 1 and s.conn_gen == 1
    (entry,) = attempt_entries(owner, "CONNECT")
    assert entry.count == 5 and entry.state == "ACTIVE", "the failures after the disconnect are not swallowed"
    assert events(owner) == ["NEW_GENERATION", "FIRST_COMM", "RESYNC"]


async def test_failed_rounds_are_not_connect_attempts_and_do_not_count_as_such():
    def silent(address, count):
        raise ModbusIOException("no response received")

    owner, ingress, build = modbus_bench(ScriptedModbus([True], silent))
    acquirer = await run_attempts(owner, ingress, build, 3)
    s = owner.sources["mb"]
    assert acquirer.connect_failures == 0 and attempt_entries(owner, "CONNECT") == []
    assert s.resync_count == 3 and s.conn_gen == 3, "three generations, each: connected, three timed-out rounds, back to resync"
    stretches = [e for e in owner.commlog.entries.values() if e.event == "POLL_TIMEOUT"]
    assert [e.count for e in stretches] == [3, 3, 3] and [e.conn_gen for e in stretches] == [1, 2, 3]


async def test_a_cancelled_connect_is_not_a_failure():
    script = ScriptedModbus([False, "stall"])
    owner, ingress, build = modbus_bench(script)
    acquirer, gate = build(10_000)
    task = asyncio.create_task(acquirer.run())
    await drain_until(ingress, lambda: script.calls == 2, why="second connect in flight")
    assert acquirer.connect_failures == 1
    await stop(task)
    assert acquirer.connect_failures == 1, "stopping the service mid-attempt is not the server's fault"


async def test_requests_are_reported_one_by_one_and_part_of_a_round_failing_does_not_hide_in_the_whole():
    served = {10: 0}

    def read(address, count):
        if address == 10:
            served[10] += 1
            if served[10] <= 2:
                raise ModbusIOException("no response received")
        return FakeResponse(list(range(count)))

    owner, ingress, build = modbus_bench(ScriptedModbus([True], read), addresses=(0, 2, 10))
    acquirer, gate = build(10_000)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: served[10] >= 4, why="four rounds")
    finally:
        await stop(task)
    ingress.drain()
    failing = [e for e in owner.commlog.entries.values() if e.event == "POLL_TIMEOUT"]
    assert [(e.scope, e.count, e.end_reason) for e in failing] == [("holding/1/10+2", 2, "SUCCESS")]
    assert acquirer.outcomes[:2] == ["OK", "OK"], "the round answered; only one of its two requests did not"
    assert not [e for e in owner.commlog.entries.values() if e.scope == "holding/1/0+4"]


async def test_the_protocol_exception_code_reaches_the_log():
    served = {"n": 0}

    def read(address, count):
        served["n"] += 1
        return ExceptionResponse(3, 2 if served["n"] <= 2 else 4)

    owner, ingress, build = modbus_bench(ScriptedModbus([True], read))
    acquirer, gate = build(10_000)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: served["n"] >= 4, why="four rounds")
    finally:
        await stop(task)
    ingress.drain()
    codes = [(e.reason, e.count) for e in owner.commlog.entries.values() if e.event == "POLL_EXCEPTION"]
    assert codes[0] == ("EXCEPTION_CODE_2", 2) and codes[1][0] == "EXCEPTION_CODE_4"


# ============================================================== OPC UA acquirer (real server where it matters)

NAMES = ["a"]


@pytest.fixture
async def field():
    f = FieldUa(NAMES)
    await f.start()
    yield f
    await f.stop()


def ua_bench(endpoint, points, *, client_factory=None, **source):
    config = Settings.model_validate(
        {
            "sources": [{"id": "ua", "kind": "opcua", "enabled": True, "endpoint": endpoint, **source}],
            "points": [p.model_dump() for p in points],
        }
    )
    owner = Owner(config, time.time(), time.monotonic(), commit=Recorder().commit, run_id="run")
    ingress = Ingress(owner, 10_000)

    def build(attempts):
        gate = Gate(attempts)
        acquirer = UaAcquirer(
            config.sources[0], list(points), config.timing, ingress, client_factory=client_factory, sleep=gate
        )
        owner.on_rebuild = acquirer.request_rebuild
        return acquirer, gate

    return owner, ingress, build


def ua_points(field, *names):
    return [Point(id=n, source="ua", unit="u", node_id=field.node_id(n)) for n in names]


async def test_five_refused_subscriptions_are_one_entry_counted_five_beside_five_resyncs(field):
    """P1/P2: the attempts are one line spanning five generations; the resyncs and generations are their own lines."""
    owner, ingress, build = ua_bench(field.endpoint, ua_points(field, "a", "missing"))
    acquirer, gate = build(5)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: gate.blocked, why="five cycles")
    finally:
        await stop(task)
    ingress.drain()
    s = owner.sources["ua"]
    assert (s.resync_count, s.conn_gen, acquirer.connect_failures) == (5, 5, 0), (
        "connect() returned each time, so no connect failure, though every subscription failed"
    )
    (entry,) = attempt_entries(owner, "SUBSCRIBE")
    assert (entry.count, entry.conn_gen, entry.state, entry.reason) == (5, None, "ACTIVE", "ERROR:SubscriptionError")
    assert "refused" in entry.detail and attempt_entries(owner, "CONNECT") == []
    assert events(owner).count("RESYNC") == 5 and events(owner).count("NEW_GENERATION") == 5
    assert not hasattr(acquirer, "history"), "the unbounded list of reasons is gone; the bounded log replaces it"


async def test_a_subscription_that_finally_opens_ends_its_failure_entry_and_writes_no_success_line(field):
    points = ua_points(field, "a", "missing")
    owner, ingress, build = ua_bench(field.endpoint, points)
    acquirer, gate = build(10_000)
    gate.on_call = lambda n: points.pop() if n == 2 and len(points) == 2 else None  # the bad item is fixed after two failures
    acquirer.points = points
    task = asyncio.create_task(acquirer.run())
    try:
        # (a failed cycle may already have heard from the server: only the third generation is the working one)
        await drain_until(ingress, lambda: owner.sources["ua"].conn_gen == 3 and owner.sources["ua"].state == "VALID", why="a working subscription")
    finally:
        await stop(task)
    ingress.drain()
    (entry,) = attempt_entries(owner, "SUBSCRIBE")
    assert (entry.count, entry.state, entry.end_reason) == (2, "ENDED", "SUBSCRIBE_OK")
    assert acquirer.connect_failures == 0
    assert [e.event for e in owner.commlog.entries.values() if e.kind == "ATTEMPT"] == ["ATTEMPT_FAILED"], "success writes no line"


async def test_a_server_that_is_not_there_at_start_is_logged_and_counted_and_the_first_connect_clears_it():
    field = FieldUa(NAMES)  # a free port, nobody listening yet
    field.index = 2  # the namespace the server will register (node ids are needed before it exists)
    owner, ingress, build = ua_bench(field.endpoint, ua_points(field, "a"))
    acquirer, gate = build(3)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: gate.blocked, why="three refused connects")
        ingress.drain()
        s = owner.sources["ua"]
        assert (acquirer.connect_failures, s.resync_count, s.conn_gen) == (3, 0, 0)
        (entry,) = attempt_entries(owner, "CONNECT")
        assert (entry.count, entry.state, entry.conn_gen, entry.phase) == (3, "ACTIVE", None, "CONNECT")
        assert entry.reason.startswith("ERROR:") and events(owner) == []
        await field.start()  # the server comes up
        gate.release.set()
        await drain_until(ingress, lambda: s.state == "VALID", why="connected and publishing")
    finally:
        await stop(task)
        await field.stop()
    ingress.drain()
    assert acquirer.connect_failures == 0 and entry.end_reason == "CONNECT_OK"
    assert events(owner)[:2] == ["NEW_GENERATION", "FIRST_COMM"]
    assert attempt_entries(owner, "SUBSCRIBE") == []


async def test_a_dropped_server_logs_its_failed_reconnects_and_the_recovery_with_its_duration(field):
    owner, ingress, build = ua_bench(field.endpoint, ua_points(field, "a"))
    acquirer, gate = build(10_000)
    task = asyncio.create_task(acquirer.run())
    s = owner.sources["ua"]
    try:
        await drain_until(ingress, lambda: s.state == "VALID", why="first connection")
        await field.stop()
        await drain_until(ingress, lambda: s.state == "RESYNC", why="the drop is noticed")
        await drain_until(ingress, lambda: acquirer.connect_failures >= 2, timeout=60, why="two refused reconnects")
        assert s.resync_count == 1, "the failed reconnects are not new resyncs"
        await field.start()
        await drain_until(ingress, lambda: s.state == "VALID" and s.conn_gen == 2, timeout=60, why="back")
    finally:
        await stop(task)
        await field.stop()
    ingress.drain()
    assert acquirer.connect_failures == 0
    (attempts,) = attempt_entries(owner, "CONNECT")
    assert attempts.count >= 2 and attempts.end_reason == "CONNECT_OK" and attempts.conn_gen is None
    recovered = entries(owner, "COMM_RECOVERED")
    assert [e.conn_gen for e in recovered] == [2] and recovered[0].as_dict()["silent_seconds"] > 0
    assert events(owner).index("RESYNC") < events(owner).index("COMM_RECOVERED")


class FakeUaClient:
    """Enough of a client for the acquirer's own steps; `fail` says which one raises."""

    def __init__(self, fail, error):
        self.fail, self.error = fail, error
        self.uaclient = type("UaClient", (), {"session": None})()

    async def set_security_string(self, text):
        if self.fail == "security":
            raise self.error

    async def connect(self):
        if self.fail == "connect":
            raise self.error
        if self.fail == "stall":
            await asyncio.sleep(3600)

    async def disconnect(self):
        return None

    def disconnect_socket(self):
        return None


async def test_a_failing_security_setup_before_connect_is_a_connect_failure():
    owner, ingress, build = ua_bench(
        "opc.tcp://127.0.0.1:1/", [Point(id="a", source="ua", unit="u", node_id="ns=2;s=a")],
        client_factory=lambda: FakeUaClient("security", ValueError("cannot read certificate C:/certs/client.pem")),
        security="Basic256Sha256,SignAndEncrypt,client.pem,key.pem",
    )  # fmt: skip
    acquirer, gate = build(2)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: gate.blocked, why="two attempts")
    finally:
        await stop(task)
    ingress.drain()
    (entry,) = attempt_entries(owner, "CONNECT")
    assert (acquirer.connect_failures, entry.count, entry.reason) == (2, 2, "ERROR:ValueError")
    assert owner.sources["ua"].resync_count == 0


async def test_the_configured_user_name_and_password_never_reach_the_log(monkeypatch):
    monkeypatch.setenv("HTSG_TEST_UA_USER", "bobby")
    monkeypatch.setenv("HTSG_TEST_UA_PASS", "zx81-secret")
    message = "rejected the sign-in of bobby with zx81-secret (url opc.tcp://bobby:zx81-secret@plant/ua)"
    owner, ingress, build = ua_bench(
        "opc.tcp://127.0.0.1:1/", [Point(id="a", source="ua", unit="u", node_id="ns=2;s=a")],
        client_factory=lambda: FakeUaClient("connect", RuntimeError(message)),
        username_env="HTSG_TEST_UA_USER", password_env="HTSG_TEST_UA_PASS",
    )  # fmt: skip
    acquirer, gate = build(1)
    task = asyncio.create_task(acquirer.run())
    try:
        await drain_until(ingress, lambda: gate.blocked, why="one attempt")
    finally:
        await stop(task)
    ingress.drain()
    (entry,) = attempt_entries(owner, "CONNECT")
    assert "bobby" not in entry.detail and "zx81-secret" not in entry.detail and "rejected the sign-in" in entry.detail
    assert "bobby" not in repr(owner.commlog.snapshot()) and "zx81-secret" not in repr(owner.commlog.snapshot())


async def test_a_connect_cancelled_in_flight_is_not_a_failure_for_opcua_either():
    calls = []

    def factory():
        calls.append(1)
        return FakeUaClient("connect" if len(calls) == 1 else "stall", ConnectionRefusedError("refused"))

    owner, ingress, build = ua_bench(
        "opc.tcp://127.0.0.1:1/", [Point(id="a", source="ua", unit="u", node_id="ns=2;s=a")], client_factory=factory
    )
    acquirer, gate = build(10_000)
    task = asyncio.create_task(acquirer.run())
    await drain_until(ingress, lambda: len(calls) == 2, why="the second attempt is under way")
    assert acquirer.connect_failures == 1
    await stop(task)
    assert acquirer.connect_failures == 1, "stopping the service mid-attempt is not the server's fault"
