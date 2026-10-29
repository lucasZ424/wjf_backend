"""R07 / Q08: a Modbus input that a failed round takes away leaves ONE record saying so (终版 §5, §10 第3条, §14).

Why it matters: until now a whole-round timeout or exception, and an unanswered request inside an otherwise answered
round, left nothing in the raw history: the earlier value was held as valid to the next sample, the segment end or the
watermark - while the derived values of that very moment said "unavailable" and the live view agreed with them.  A
history that disagrees with the live view about which values were valid is not an honest one.  The record is an
observation without a value whose quality names the cause (MISSING_TIMEOUT / _EXCEPTION / _CONNECTION for a whole round,
_UNANSWERED for a request of an otherwise answered round), written when the input turns from valid to invalid and not
again while it stays so.  Every scenario is checked on the owner's output held in memory and on a real database.
"""

from dataclasses import asdict

import pytest
from owner_support import FixedBucket, Recorder, make_owner, notify, poll
from protocol_support import FieldModbusDevices, bridge_points, device_points
from test_partial_requests import Live, until
from test_source_validity import WALL0

from htsg import history as h
from htsg import records as r
from htsg.config import Settings
from htsg.owner import Owner

CHANNELS = {"b1": "mb", "s1": "mb"}


def memory_read(rec, channels):
    """What a database would hand the query: the groups the owner committed, row for row (of the asked channels)."""
    read = h.StoreRead("memory")
    segments = {}
    for group in rec.commits:
        for s in group.states:
            key = (s.run_id, s.source_id, s.seg)
            if s.kind == "SEG_START":
                segments[key] = {
                    "run_id": s.run_id, "source_id": s.source_id, "seg": s.seg, "conn_gen": s.detail.get("conn_gen"),
                    "start_wall": s.at_wall, "start_reason": s.detail.get("reason"), "end_wall": None, "end_reason": None,
                }  # fmt: skip
            elif s.kind == "SEG_END" and key in segments:
                segments[key]["end_wall"], segments[key]["end_reason"] = s.detail.get("end_wall", s.at_wall), s.detail.get("reason")
            elif s.kind == "WATERMARK":
                read.watermarks.append(
                    {"run_id": s.run_id, "source_id": s.source_id, "seg": s.detail["seg"], "covered_wall": s.detail["covered_wall"]}
                )
        for o in (o for o in group.observations if o.point_id in channels):
            read.rows.append({**asdict(o), "effective_wall": o.received_wall if o.asserted_wall is None else o.asserted_wall, "kind": "raw"})
        for x in (x for x in group.results if x.point_id in channels):
            read.rows.append({**asdict(x), "effective_wall": x.at_wall if x.asserted_wall is None else x.asserted_wall, "kind": "derived"})
    read.segments = list(segments.values())
    return read


def in_memory(rec, channels, start, end):
    return h.assemble([memory_read(rec, channels)], channels, start, end)


def on_database(factory, rec, config, channels, start, end):
    store = factory()
    store.initialize(config)
    store.bind_run("run", config.config_id, WALL0)
    for group in rec.commits:
        assert store.write_group(group).durable, group.group_id
    return store, h.query([("primary", store)], channels, start, end)


def raw(result, pid):
    return [x for span in result["channels"][pid]["segments"] for x in span["rows"] if x["kind"] == "raw"]


def derived(result, pid):
    return [x for span in result["channels"][pid]["segments"] for x in span["rows"] if x["kind"] == "derived"]


def rnd(owner, tick, values=None, outcome="OK", **kw):
    """One Modbus round at second `tick` (wall 1000+tick, monotonic 100+tick), then the owner's tick."""
    batch = poll(tick, {"b1": 0.5, "b2": 0.5} if values is None else values, outcome=outcome, **kw)
    owner.accept(batch)
    owner.tick(batch.wall, batch.mono)
    return batch


def missing(rec):
    return [o for o in rec.observations() if o.quality.startswith("MISSING_")]


# ============================================================================================ ① a failed round


def check_one_timeout(result):
    b1 = raw(result, "b1")
    assert [(x["time"], x["quality"], x["valid"]) for x in b1] == [
        (1010.0, "GOOD", True), (1011.0, "MISSING_TIMEOUT", False), (1012.0, "GOOD", True),
    ], "the failure is in the history, as an invalid moment of its own"  # fmt: skip
    assert (b1[0]["until"], b1[0]["ended_by"]) == (1011.0, "NEXT_CHANGE"), "the value of 1010 holds only to the failed round"
    assert b1[1]["value"] is None and b1[1]["until"] == 1012.0
    s1 = derived(result, "s1")
    assert [(x["time"], x["valid"]) for x in s1] == [(1010.0, True), (1011.0, False), (1012.0, True)]
    assert (b1[1]["valid"], s1[1]["valid"]) == (False, False), "raw and derived agree at the same moment"


def test_a_valid_input_that_a_whole_round_timeout_takes_away_gets_one_record_and_history_ends_its_value_there(archive_db_factory):
    owner, rec = make_owner()
    rnd(owner, 10)
    rnd(owner, 11, {}, "TIMEOUT")
    live = owner.channel_views()
    rnd(owner, 12, {"b1": 0.6, "b2": 0.6})
    failed = missing(rec)
    assert sorted(o.point_id for o in failed) == ["b1", "b2"] and {o.quality for o in failed} == {"MISSING_TIMEOUT"}
    one = next(o for o in failed if o.point_id == "b1")
    assert (one.value, one.status_code, one.extreme, one.origin) == (None, None, False, r.ORIGIN_RECEIVED)
    assert (one.received_wall, one.tick, one.poll_id, one.response_id) == (1011.0, 11, "poll11", None)
    group = next(g for g in rec.commits if one in g.observations)
    assert {s.kind for s in group.states} >= {"POLL"} and {x.point_id for x in group.results} >= {"s1", "s2"}, "one group"
    assert [o.item_seq for o in group.observations] == [0, 1] and one.batch_seq == group.observations[0].batch_seq
    assert (live["b1"]["valid"], live["b1"]["reason"], live["s1"]["status"]) == (False, "POLL_TIMEOUT", r.RESULT_NA), "live view"
    assert (live["b1"]["value"], live["b1"]["quality"]) == (None, "MISSING_TIMEOUT")
    check_one_timeout(in_memory(rec, CHANNELS, 1009.0, 1013.0))
    store, result = on_database(archive_db_factory, rec, owner.config, CHANNELS, 1009.0, 1013.0)
    check_one_timeout(result)
    # input_basis: the result of the failed round rests on an input that has only a failure record there
    with store.session() as conn:
        result_id = conn.execute("SELECT id FROM result WHERE point_id='s1' AND at_wall=1011").fetchone()["id"]
    basis = h.input_basis([("primary", store)], result_id)
    assert basis["missing"] == ["b1"] and basis["complete"] is False
    assert [x["quality"] for x in basis["inputs"]["b1"]["rows"]] == ["MISSING_TIMEOUT"]


def test_a_second_failed_round_in_a_row_adds_no_record():
    owner, rec = make_owner()
    rnd(owner, 10)
    rnd(owner, 11, {}, "TIMEOUT")
    rnd(owner, 12, {}, "TIMEOUT")
    rnd(owner, 13, {}, "TIMEOUT")
    assert sorted((o.point_id, o.received_wall) for o in missing(rec)) == [("b1", 1011.0), ("b2", 1011.0)]
    rnd(owner, 14, {"b1": 0.7, "b2": 0.7})
    rnd(owner, 15, {}, "TIMEOUT")
    assert len(missing(rec)) == 4, "a valid observation in between makes the next failure a new transition"


def test_after_the_failure_the_first_valid_observation_starts_the_value_again(archive_db_factory):
    owner, rec = make_owner()
    rnd(owner, 10)
    rnd(owner, 11, {}, "TIMEOUT")
    rnd(owner, 12, {}, "TIMEOUT")
    rnd(owner, 13, {"b1": 0.9, "b2": 0.9})
    for result in (in_memory(rec, CHANNELS, 1009.0, 1020.0), on_database(archive_db_factory, rec, owner.config, CHANNELS, 1009.0, 1020.0)[1]):
        b1 = raw(result, "b1")
        assert [(x["time"], x["valid"]) for x in b1] == [(1010.0, True), (1011.0, False), (1013.0, True)]
        assert b1[1]["until"] == 1013.0, "the failure ends where the next valid observation begins"


# ======================================================================================= ② a protocol exception


def check_exception(result):
    b1 = raw(result, "b1")
    assert [(x["time"], x["quality"], x["valid"]) for x in b1] == [(1001.0, "GOOD", True), (1002.0, "MISSING_EXCEPTION", False)]
    assert (b1[0]["until"], b1[0]["ended_by"]) == (1002.0, "NEXT_CHANGE"), "not held on through the sixty seconds to the watermark"


def test_a_protocol_exception_that_goes_on_for_over_a_minute_leaves_one_record_and_ends_the_earlier_value_at_its_start(archive_db_factory):
    owner, rec = make_owner()
    rnd(owner, 1)
    for tick in range(2, 64):
        rnd(owner, tick, {}, "EXCEPTION")
    assert sorted((o.point_id, o.quality, o.received_wall) for o in missing(rec)) == [
        ("b1", "MISSING_EXCEPTION", 1002.0), ("b2", "MISSING_EXCEPTION", 1002.0),
    ]  # fmt: skip
    assert owner.sources["mb"].state == "VALID" and not owner.sources["mb"].alarm, "an answering device is alive"
    assert owner.channel_views()["b1"]["reason"] == "POLL_EXCEPTION", "the reason shown is the poll's, not QUALITY"
    check_exception(in_memory(rec, CHANNELS, 1000.0, 1070.0))
    store, result = on_database(archive_db_factory, rec, owner.config, CHANNELS, 1000.0, 1070.0)
    check_exception(result)
    (span,) = result["channels"]["b1"]["segments"]
    assert span["covered_until"] > 1050.0 and result["coverage_end"]["mb"] == span["covered_until"], "the watermark is unaffected"


# ================================================================================== ③ a request of a round unanswered


def two_devices():
    config = Settings.model_validate(
        {
            "sources": [{"id": "mb", "kind": "modbus", "enabled": True, "host": "127.0.0.1", "port": 15020}],
            "points": [p.model_dump() for p in device_points()],
        }
    )
    recorder = Recorder()
    owner = Owner(config, 1000.0, 100.0, commit=recorder.commit, buckets=FixedBucket(), run_id="run")
    owner.begin_generation("mb", 1, 1000.0, 100.0)
    return config, owner, recorder


def device_round(owner, tick, answered, outcome="OK"):
    """A round of two requests (one per device); `answered` lists the devices that answered."""
    items = tuple(
        r.Item(f"d{d}_{k}", 0.5 + d, r.GOOD, None, None, f"poll{tick}/{d}", 1000.0 + tick, 100.0 + tick)
        for d in answered
        for k in "ab"
    )
    requests = tuple(r.RequestResult(f"holding/{d}/0+4", "OK" if d in answered else "TIMEOUT", None, None) for d in (1, 2))
    batch = r.InputBatch(
        "mb", 1, r.KIND_MODBUS_POLL, 1000.0 + tick, 100.0 + tick, items, tick=tick, poll_id=f"poll{tick}", outcome=outcome,
        requests=requests,
    )  # fmt: skip
    owner.accept(batch)
    owner.tick(batch.wall, batch.mono)


def test_an_unanswered_request_of_an_answered_round_gives_its_inputs_one_record_each_and_the_answered_ones_stay_valid(archive_db_factory):
    config, owner, recorder = two_devices()
    device_round(owner, 1, (1, 2))
    for tick in (2, 3, 4, 5):
        device_round(owner, tick, (1,))  # the second device never answers
    failed = missing(recorder)
    assert sorted((o.point_id, o.quality, o.received_wall) for o in failed) == [
        ("d2_a", "MISSING_UNANSWERED", 1002.0), ("d2_b", "MISSING_UNANSWERED", 1002.0),
    ]  # fmt: skip
    live = owner.channel_views()
    assert (live["d1_a"]["valid"], live["d2_a"]["valid"], live["d2_a"]["reason"]) == (True, False, "POLL_MISSING")
    group = next(g for g in recorder.commits if failed[0] in g.observations)
    assert [o.item_seq for o in group.observations] == [0, 1, 2, 3], "the failures follow the round's own observations"
    channels = {"d1_a": "mb", "d2_a": "mb"}
    device_round(owner, 6, (1, 2))  # ④ recovery
    for result in (
        in_memory(recorder, channels, 1000.0, 1010.0),
        on_database(archive_db_factory, recorder, config, channels, 1000.0, 1010.0)[1],
    ):
        d2 = raw(result, "d2_a")
        assert [(x["time"], x["quality"], x["valid"]) for x in d2] == [
            (1001.0, "GOOD", True), (1002.0, "MISSING_UNANSWERED", False), (1006.0, "GOOD", True),
        ]  # fmt: skip
        assert d2[0]["until"] == 1002.0 and d2[1]["until"] == 1006.0
        assert [x["time"] for x in raw(result, "d1_a")] == [1001.0, 1002.0, 1003.0, 1004.0, 1005.0, 1006.0], "answered: every round"


@pytest.mark.parametrize("outcome", ["TIMEOUT", "EXCEPTION", "CONNECTION"])
def test_every_whole_round_failure_names_its_cause_and_the_live_reason_stays_the_polls(outcome):
    owner, rec = make_owner()
    rnd(owner, 10)
    rnd(owner, 11, {}, outcome)
    assert {o.quality for o in missing(rec)} == {"MISSING_" + outcome}
    assert owner.channel_views()["b1"]["reason"] == "POLL_" + outcome, "not QUALITY: the live view says what it said before"


def collapsed(items):
    """Consecutive repeats made one: the shape of a series."""
    return [x for i, x in enumerate(items) if i == 0 or x != items[i - 1]]


async def test_real_slaves_one_that_stops_answering_gives_its_inputs_one_record_and_the_other_stays_valid(archive_db_factory):
    """细则 7.2 ③ on the real wire: a real acquirer, two real slaves, one of which goes silent for six rounds and comes back."""
    server = FieldModbusDevices()
    await server.start()

    def next_round(offered):
        server.mode[2] = FieldModbusDevices.SILENT if 3 <= offered < 9 else FieldModbusDevices.OK

    try:
        async with Live(server, on_round=next_round) as live:
            await until(lambda: len(live.rounds) >= 14, 30)
    finally:
        await server.stop()
    assert live.rounds[3:9] == ["OK"] * 6, "the round is answered although one request is not"
    recorder = live.recorder
    failed = missing(recorder)
    assert sorted((o.point_id, o.quality) for o in failed) == [("d2_a", "MISSING_UNANSWERED"), ("d2_b", "MISSING_UNANSWERED")]
    assert len({o.batch_seq for o in failed}) == 1, "one failure, one round, however long the silence goes on"
    walls = [o.received_wall for o in recorder.observations()]
    channels = {"d1_a": "mb", "d2_a": "mb"}
    start, end = min(walls) - 1.0, max(walls) + 1.0
    for result in (
        in_memory(recorder, channels, start, end),
        on_database(archive_db_factory, recorder, live.config, channels, start, end)[1],
    ):
        d2 = raw(result, "d2_a")
        assert collapsed([x["quality"] for x in d2]) == ["GOOD", "MISSING_UNANSWERED", "GOOD"]
        before = [x for x in d2 if x["time"] < failed[0].received_wall][-1]
        assert (before["until"], before["ended_by"]) == (failed[0].received_wall, "NEXT_CHANGE"), "the value ends where it went missing"
        assert {x["quality"] for x in raw(result, "d1_a")} == {"GOOD"}, "the answering slave never loses a moment"


# ============================================================= nothing changes where nothing was taken away


def test_an_input_that_was_already_invalid_or_never_observed_gets_no_record():
    owner, rec = make_owner()
    rnd(owner, 1, {"b1": float("nan"), "b2": 0.5})  # b1 is decoded as Bad: a Bad observation, as before
    bad = next(o for o in rec.observations("b1"))
    assert (bad.quality, bad.value) == (r.BAD, None)
    rnd(owner, 2, {}, "TIMEOUT")
    assert [(o.point_id, o.quality) for o in missing(rec)] == [("b2", "MISSING_TIMEOUT")], "b1 was invalid already"
    fresh, recorder = make_owner()
    rnd(fresh, 1, {}, "TIMEOUT")  # nothing was ever valid
    assert missing(recorder) == []


def test_opc_ua_paths_are_untouched_and_never_produce_a_failure_record():
    owner, rec = make_owner()
    owner.accept(notify([("ti", 480.0), ("to", 450.0)], wall=1005.0, mono=105.0, seq=1))
    owner.accept(notify([("ti", None, r.BAD, 0x80000000)], wall=1006.0, mono=106.0, seq=2))
    owner.tick(1006.0, 106.0)
    qualities = [(o.point_id, o.quality, o.value) for o in rec.observations() if o.source_id == "ua"]
    assert qualities == [("ti", "GOOD", 480.0), ("to", "GOOD", 450.0), ("ti", "BAD", None)]
    assert missing(rec) == []


def test_the_state_re_statement_of_a_failed_input_says_what_is_true_now_not_the_value_before():
    owner, rec = make_owner()
    rnd(owner, 10)
    rnd(owner, 11, {}, "TIMEOUT")
    failure = next(o for o in missing(rec) if o.point_id == "b1")
    observations, _ = owner._baseline(owner.sources["mb"], 1020.0, 120.0)
    restated = next(o for o in observations if o.point_id == "b1")
    assert (restated.orig_id, restated.quality, restated.value, restated.origin) == (failure.id, "MISSING_TIMEOUT", None, r.ORIGIN_ASSERTION)


def test_the_communication_page_row_of_a_failed_input_has_no_value_a_missing_quality_and_keeps_its_reason():
    owner, rec = make_owner()
    rnd(owner, 10)
    rnd(owner, 11, {}, "TIMEOUT")
    failure = next(o for o in missing(rec) if o.point_id == "b1")
    row = next(x for x in owner.communication_inputs() if x["point_id"] == "b1")
    assert (row["observation_id"], row["value"], row["quality"], row["valid"], row["reason"]) == (
        failure.id, None, "MISSING_TIMEOUT", False, "POLL_TIMEOUT",
    )  # fmt: skip


# ================================================================================================ capacity


def test_at_full_scale_one_failure_adds_at_most_eight_records_and_a_failure_that_goes_on_adds_none():
    points = bridge_points(8)
    config = Settings.model_validate(
        {
            "sources": [{"id": "mb", "kind": "modbus", "enabled": True, "host": "127.0.0.1", "port": 15020}],
            "points": [p.model_dump() for p in points],
        }
    )
    recorder = Recorder()
    owner = Owner(config, 1000.0, 100.0, commit=recorder.commit, buckets=FixedBucket(), run_id="run")
    owner.begin_generation("mb", 1, 1000.0, 100.0)
    ids = [p.id for p in points]
    for tick, (values, outcome) in enumerate([({i: 0.5 for i in ids}, "OK"), ({}, "TIMEOUT"), ({}, "TIMEOUT")], start=1):
        owner.accept(poll(tick, values, outcome=outcome))
    assert len(missing(recorder)) == 8, "8 Modbus inputs: at most 8 records for one failure, none for the rounds after it"
