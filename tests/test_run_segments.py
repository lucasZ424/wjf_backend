"""D22: run, connection and archive-segment lifecycle (终版 §5, §11).

Why it matters: history is cut into valid intervals by exactly these records.  A run that never closed,
or a clock step that did not open a segment, would let a stale value look valid across a discontinuity.
"""

import pytest
from archive_support import groups_in_order, samples
from legacy_support import make_legacy
from owner_support import FixedBucket, Recorder, notify
from test_source_validity import MONO0, WALL0, at, data, ua_only

from htsg import records as r
from htsg.archive_router import ArchiveRouter
from htsg.owner import Owner

CONFIG = ua_only()


def owner_and_groups(config=None, run_id="run", *, confirm=False):
    rec = Recorder()
    owner = Owner(config or CONFIG, WALL0, MONO0, run_id=run_id, buckets=FixedBucket())

    def commit(group):
        rec.commit(group)
        if confirm:
            owner.committed(group)
        return True

    owner.commit = commit  # this unit-test sink behaves like a writer that confirms every accepted group immediately
    owner.start(WALL0, MONO0)
    owner.begin_generation("ua", 1, WALL0, MONO0)
    return owner, rec


def stored(archive_db_factory, rec, run_id="run", config=None):
    cfg = config or CONFIG
    store = archive_db_factory()
    store.initialize(cfg)
    store.bind_run(run_id, cfg.config_id, WALL0)
    for group in rec.commits:  # in the owner's order, the order the writer would use
        assert store.write_group(group).durable, group.group_id
    return store


def one(store, sql, *args):
    with store.session() as conn:
        return conn.execute(sql, args).fetchall()


def test_start_registers_the_run_and_a_normal_stop_closes_every_segment_after_the_last_record(archive_db_factory):
    owner, rec = owner_and_groups(confirm=True)
    data(owner, 101.0)
    owner.tick(*at(102.0))
    owner.shutdown(*at(103.0))
    store = stored(archive_db_factory, rec)
    (run,) = one(store, "SELECT config_id,started_wall,ended_wall,end_reason FROM run")
    assert run["config_id"] == CONFIG.config_id and run["started_wall"] == WALL0
    assert (run["ended_wall"], run["end_reason"]) == (at(103.0)[0], "NORMAL")
    assert one(store, "SELECT 1 FROM segment WHERE end_wall IS NULL AND start_wall IS NOT NULL") == []
    with store.session() as conn:
        last = groups_in_order(conn, "g.order_no")[-1]
    assert last["kind"] == "RUN_END", "the end is registered after everything accepted was processed"
    assert rec.commits[-1].states[0].kind == "RUN_END"


def test_an_exit_without_a_stop_is_closed_at_the_last_persisted_evidence_and_nothing_later(archive_db_factory):
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    for second in range(102, 125):  # keep-alives and the 10 s watermarks
        wall, mono = at(float(second))
        owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, wall, mono))
        owner.tick(wall, mono)
    store = stored(archive_db_factory, rec)  # the process "dies" here: no shutdown
    assert one(store, "SELECT ended_wall FROM run")[0]["ended_wall"] is None
    expected = max(
        max(o.effective for o in [type("E", (), {"effective": o.asserted_wall or o.received_wall})() for o in rec.observations()]),
        max(s.detail["covered_wall"] for g in rec.commits for s in g.states if s.kind == "WATERMARK"),
        max(s.at_wall for g in rec.commits for s in g.states if s.kind != "CLOCK_JUMP"),
    )  # fmt: skip
    segment_expected = max(  # R03a: a segment ends at ITS OWN evidence - a valid observation or its watermark, no state record
        max(o.asserted_wall or o.received_wall for o in rec.observations() if o.quality == r.GOOD and not o.extreme),
        max(s.detail["covered_wall"] for g in rec.commits for s in g.states if s.kind == "WATERMARK"),
    )
    closed = store.reconcile_unfinished_runs("a-new-run")
    assert closed == [{"run_id": "run", "ended_wall": expected, "segments_cut": 1}]
    (run,) = one(store, "SELECT ended_wall,end_reason FROM run")
    assert (run["ended_wall"], run["end_reason"]) == (expected, "ABNORMAL_EXIT")
    (segment,) = one(store, "SELECT end_wall,end_reason FROM segment")
    assert (segment["end_wall"], segment["end_reason"]) == (segment_expected, "EXIT_TRUNCATED")
    assert expected < at(124.0)[0] + 1, "no crash time is invented beyond the evidence"
    assert store.reconcile_unfinished_runs("a-new-run") == [], "idempotent"


def test_the_current_run_and_already_ended_runs_are_left_alone(archive_db_factory):
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    store = stored(archive_db_factory, rec)
    assert store.reconcile_unfinished_runs("run") == [], "a run that is still the live one is never closed"
    assert one(store, "SELECT ended_wall FROM run")[0]["ended_wall"] is None


@pytest.mark.parametrize(
    ("wall_step", "mono_step", "opens"),
    [(3.0, 1.0, False), (3.01, 1.0, True), (-19.0, 1.0, True), (1.0, 1.0, False), (-0.5, 1.0, False)],
    ids=["exactly-2s", "just-over-2s", "step-back-20s", "no-step", "step-back-1.5s"],
)
def test_a_wall_vs_monotonic_difference_above_two_seconds_opens_a_segment_exactly_two_does_not(
    wall_step, mono_step, opens
):
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    owner.tick(*at(101.0))
    seg_before = owner.sources["ua"].seg
    owner.tick(WALL0 + 1.0 + wall_step, MONO0 + 1.0 + mono_step)
    assert (owner.sources["ua"].seg == seg_before + 1) is opens
    if opens:
        jump = rec.states("CLOCK_JUMP")[-1]
        assert abs(jump.detail["delta"]) > 2


def test_a_step_back_keeps_both_halves_apart_by_segment_and_re_states_the_values(archive_db_factory):
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    owner.tick(*at(101.0))
    owner.tick(WALL0 + 1.0 - 20.0, MONO0 + 2.0)  # wall clock jumped back 20 s while 1 s passed
    owner.accept(notify([("ti", 510.0)], wall=WALL0 - 17.5, mono=MONO0 + 3.0, seq=2))  # stamped by the new clock
    assert owner.sources["ua"].seg == 2
    old, new = [o for o in rec.observations("ti") if o.origin == r.ORIGIN_RECEIVED]
    assert new.received_wall < old.received_wall, "the wall label really went backwards"
    assert (old.seg, new.seg) == (1, 2), "overlapping or reversed wall times stay separate by segment"
    restated = [o for o in rec.observations() if o.origin == r.ORIGIN_ASSERTION]
    assert {o.point_id for o in restated} == {"ti", "to"} and all(o.seg == 2 for o in restated)
    end = [s for s in rec.states("SEG_END") if s.detail["reason"] == "CLOCK_JUMP"][0]
    assert end.detail["end_wall"] == at(101.0)[0], "the old segment ends at the last wall time seen before the jump"
    store = stored(archive_db_factory, rec)
    with store.session() as conn:  # the restatements are first in their order (-1); the receipts are the others
        ordered = samples(conn, "c.point_id='ti' AND s.item>=0", order="s.seg,s.t")
    assert [x["seg"] for x in ordered] == [1, 2] and ordered[1]["t"] < ordered[0]["t"], (
        "the later receipt has the earlier wall time: the segment, not the wall, keeps them apart"
    )


def test_a_legacy_database_is_never_read_from_or_written_to(archive_db_factory):
    legacy = archive_db_factory(prepare=False)
    make_legacy(legacy, CONFIG)
    fresh = archive_db_factory("emergency")
    router = ArchiveRouter(CONFIG, legacy, fresh)
    router.initialize()
    assert router.health["primary"].startswith("REFUSED") and "primary" not in router.ready
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    result = router.write_group(rec.commits[0])
    assert result.status == "FAILED" and result.reason == "LegacyDatabaseError"
    assert one(legacy, "SELECT to_regclass('public.observation') AS t")[0]["t"] is None
