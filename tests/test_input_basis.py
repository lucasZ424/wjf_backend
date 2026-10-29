"""D33: which observations a result rests on (终版 §10).

Why it matters: "recompute nothing, but let anyone verify" only works if the stored basis can be found, is
the right one, and says so loudly when it is missing - never quietly borrowing another round or segment.
"""

from owner_support import FixedBucket, Recorder, notify, poll, small_config

from htsg import history as h
from htsg import records as r
from htsg.owner import Owner

WALL0, MONO0 = 1000.0, 100.0


def build(archive_db_factory, config=None, *, groups_hook=None):
    cfg = config or small_config()
    rec = Recorder()
    owner = Owner(cfg, WALL0, MONO0, commit=rec.commit, publish=rec.publish, buckets=FixedBucket(), run_id="run")
    for source in ("mb", "ua"):
        owner.begin_generation(source, 1, WALL0, MONO0)
    return owner, rec, cfg


def persist(archive_db_factory, rec, cfg, skip=()):
    store = archive_db_factory()
    store.initialize(cfg)
    store.bind_run("run", cfg.config_id, WALL0)
    for group in rec.commits:
        if group.group_id not in skip:
            assert store.write_group(group).durable
    return store


def result_id(rec, point, nth=-1):
    return [x.id for x in rec.results(point)][nth]


def basis(store, rid):
    return h.input_basis([("primary", store)], rid)


def test_modbus_basis_is_this_round_only_and_a_missing_input_is_reported_not_borrowed(archive_db_factory):
    owner, rec, cfg = build(archive_db_factory)
    owner.accept(poll(1, {"b1": 0.6, "b2": 0.6}))
    owner.accept(poll(2, {"b1": 0.9}))  # b2 not answered this round
    store = persist(archive_db_factory, rec, cfg)
    ok = basis(store, result_id(rec, "s1", 0))
    assert ok["complete"] and [x["batch_seq"] for x in ok["inputs"]["b1"]["rows"]] == [1]
    na = basis(store, result_id(rec, "s2"))  # s2 became NOT_AVAILABLE in round 2
    assert na["result"]["status"] == "NOT_AVAILABLE" and na["result"]["batch_seq"] == 2
    # R07: the unanswered input has a failure record of its own for round 2; it is still missing, and round 1's value is not borrowed
    assert [(x["batch_seq"], x["quality"]) for x in na["inputs"]["b2"]["rows"]] == [(2, "MISSING_UNANSWERED")]
    assert na["missing"] == ["b2"] and na["complete"] is False
    assert all(x["batch_seq"] == 2 for x in basis(store, result_id(rec, "s1", -1))["inputs"]["b1"]["rows"])


def test_strain_change_names_the_current_and_the_previous_planned_round(archive_db_factory):
    owner, rec, cfg = build(archive_db_factory)
    for tick, value in enumerate((0.6, 0.9, 1.3), start=1):
        owner.accept(poll(tick, {"b1": value, "b2": 0.6}))
    store = persist(archive_db_factory, rec, cfg)
    found = basis(store, result_id(rec, "c1"))  # the increment of round 3 over round 2
    assert found["result"]["basis"]["tick"] == 3 and found["result"]["basis"]["previous_tick"] == 2
    rows = found["inputs"]["b1"]["rows"]
    assert [(x["batch_seq"], x["tick"], x["value"]) for x in rows] == [(2, 2, 0.9), (3, 3, 1.3)]
    assert found["previous_round"]["batch_seq"] == 2 and found["complete"]


def test_opcua_basis_is_the_state_at_the_end_of_the_batch_with_every_version_in_batch_order(archive_db_factory):
    owner, rec, cfg = build(archive_db_factory)
    owner.accept(notify([("to", 450.0), ("ti", 500.0)], wall=1001.0, mono=101.0, seq=1))
    owner.accept(notify([("ti", 490.0), ("ti", 530.0)], wall=1002.0, mono=102.0, seq=2))  # 'to' not in this batch
    store = persist(archive_db_factory, rec, cfg)
    found = basis(store, result_id(rec, "dt"))
    ti, to = found["inputs"]["ti"], found["inputs"]["to"]
    assert [(x["item_seq"], x["value"]) for x in ti["rows"]] == [(0, 490.0), (1, 530.0)]
    assert ti["batch_end"] == ti["rows"][-1]["id"], "the last version is the one the calculation used"
    assert [(x["batch_seq"], x["value"]) for x in to["rows"]] == [(1, 450.0)], "state carried from the earlier batch"
    assert found["result"]["value"] == 80.0 and found["complete"]


def test_a_quality_change_with_an_unchanged_number_leaves_three_distinct_traceable_records(archive_db_factory):
    owner, rec, cfg = build(archive_db_factory)
    owner.accept(notify([("ti", 500.0), ("to", 450.0)], wall=1001.0, mono=101.0, seq=1))
    owner.accept(notify([("ti", 500.0, r.BAD, 0x80350000)], wall=1002.0, mono=102.0, seq=2))
    owner.accept(notify([("ti", 500.0)], wall=1003.0, mono=103.0, seq=3))
    store = persist(archive_db_factory, rec, cfg)
    dts = [x.id for x in rec.results("dt")]
    assert len(dts) == 3, "OK, then NOT_AVAILABLE, then OK again: the same number, three results"
    first, bad, again = (basis(store, d) for d in dts)
    assert [first["result"]["status"], bad["result"]["status"], again["result"]["status"]] == ["OK", "NOT_AVAILABLE", "OK"]
    ids = [b["inputs"]["ti"]["rows"][-1] for b in (first, bad, again)]
    assert [x["quality"] for x in ids] == ["GOOD", "BAD", "GOOD"]
    assert ids[2]["id"] != ids[0]["id"], "the later Good value is its own record, not the earlier Good one"
    assert ids[1]["status_code"] == 0x80350000 and ids[1]["value"] == 500.0


def test_values_derived_inside_an_archive_gap_say_that_their_basis_is_incomplete(archive_db_factory):
    from test_archive_gap import Sink

    sink = Sink()
    cfg = small_config()
    rec = Recorder()
    owner = Owner(cfg, WALL0, MONO0, commit=sink, publish=rec.publish, buckets=FixedBucket(), run_id="run")
    sink.owner = owner
    for source in ("mb", "ua"):
        owner.begin_generation(source, 1, WALL0, MONO0)
    owner.accept(notify([("ti", 500.0), ("to", 450.0)], wall=1001.0, mono=101.0, seq=1))
    sink.accept = False
    owner.accept(notify([("ti", 520.0)], wall=1002.0, mono=102.0, seq=2))  # lost with its result dt=70
    sink.accept = True
    owner.tick(1003.0, 103.0)
    store = archive_db_factory()
    store.initialize(cfg)
    store.bind_run("run", cfg.config_id, WALL0)
    for group in sink.groups:
        assert store.write_group(group).durable
    asserted = next(x for g in sink.groups for x in g.results if x.origin == r.ORIGIN_ASSERTION and x.point_id == "dt")
    found = basis(store, asserted.id)
    assert found["complete"] is False and "INPUTS_LOST_IN_ARCHIVE_GAP" in found["notes"]
    assert "RESTATED_RESULT" in found["notes"] and asserted.value == 70.0
    rows = [x for x in found["inputs"]["ti"]["rows"] if x["batch_seq"] == 2]
    assert [x["origin"] for x in rows] == ["STATE_ASSERTION"], "only the restatement remains; the receipt is gone"
    assert any(n.startswith("ORIGINAL_OBSERVATION_LOST") for n in found["notes"])


def test_a_configuration_version_that_cannot_be_found_is_reported(archive_db_factory):
    owner, rec, cfg = build(archive_db_factory)
    owner.accept(poll(1, {"b1": 0.6, "b2": 0.6}))
    store = persist(archive_db_factory, rec, cfg)
    with store.session() as conn:
        conn.execute("ALTER TABLE result DROP CONSTRAINT result_config_id_fkey")
        conn.execute("ALTER TABLE run DROP CONSTRAINT run_config_id_fkey")
        conn.execute("DELETE FROM config_version")
    found = basis(store, result_id(rec, "s1"))
    assert found["complete"] is False and found["notes"] == ["CONFIGURATION_VERSION_NOT_FOUND"]


def test_an_unknown_result_is_reported_as_not_found(archive_db_factory):
    owner, rec, cfg = build(archive_db_factory)
    store = persist(archive_db_factory, rec, cfg)
    assert basis(store, "nope")["found"] is False
