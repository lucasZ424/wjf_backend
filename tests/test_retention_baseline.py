"""The retention-boundary baseline (修复轮09 09e, 终版 §5 保留边界基线, §13.6 保留边界基线 / 补基线).

Why it matters: when the oldest buckets are deleted, a value that has not changed for weeks would vanish from history with
them.  So when a bucket first reaches a database, the state owner takes, at a batch boundary, every source's current values
and the derived results computed from them, and stores them as ONE group in that bucket: the bucket then begins with the
state in force.  The baseline is not a new sample - it starts no change and no fault timer, and it is never confused with the
records it restates (new ids, the originals referenced).  A baseline that could not be stored only costs the bucket its
deletion eligibility: it is tried again at a fixed pace, and it never turns ordinary archived data into a gap.
"""

from dataclasses import replace

import pytest
from owner_support import FixedBucket, Recorder, small_config
from test_archive_router import Disks, config
from test_archive_write import make_group

from htsg import records as r
from htsg.archive_router import ArchiveRouter, RouteResult
from htsg.owner import BASELINE_SOURCE, Owner

WALL0, MONO0 = 1000.0, 100.0


class Clock(FixedBucket):
    def __init__(self, no=1):
        self.no = no

    def label(self):
        return self.no


def running_owner(clock=None):
    """A state owner with both sources valid, a few rounds behind it."""
    recorder = Recorder()
    owner = Owner(small_config(), WALL0, MONO0, commit=recorder.commit, publish=recorder.publish, run_id="run",
                  buckets=clock or Clock())  # fmt: skip
    owner.start(WALL0, MONO0)
    for source in ("mb", "ua"):
        owner.begin_generation(source, 1, WALL0, MONO0)
    for n in range(1, 4):
        owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, WALL0 + n, MONO0 + n, (r.Item("ti", 480.0 + n), r.Item("to", 450.0)),
                                  publish_seq=n))  # fmt: skip
        owner.tick(WALL0 + n, MONO0 + n)
    return owner, recorder


def baselines(recorder):
    return [g for g in recorder.commits if g.kind == r.GROUP_BASELINE]


def committed_to(target, lacks=True):
    return RouteResult("COMMITTED", target, None, lacks_baseline=lacks)


# ------------------------------------------------------------------------------------------- what it holds


def test_a_baseline_restates_the_current_values_as_one_group_and_changes_nothing_in_the_owner():
    owner, recorder = running_owner()
    emitted = {rule: state.emitted for rule, state in owner.results.items()}
    held = {pid: point.obs for pid, point in owner.points.items()}
    group = owner.retention_baseline(WALL0 + 10, MONO0 + 10)
    assert group.kind == r.GROUP_BASELINE and group.source_id == BASELINE_SOURCE and group.bucket == 1
    ti = next(o for o in group.observations if o.point_id == "ti")
    assert ti.origin == r.ORIGIN_BASELINE and ti.value == 483.0 and ti.orig_id == held["ti"].id != ti.id
    assert ti.asserted_wall == WALL0 + 10 and ti.received_wall == held["ti"].received_wall, "counts from the boundary only"
    assert all(x.origin == r.ORIGIN_BASELINE and x.orig_id and x.id != x.orig_id for x in group.results)
    (state,) = [st for st in group.states if st.kind == "RETENTION_BASELINE"]
    assert state.detail["sources"] == {"mb": "NO_VALID_SEGMENT", "ua": "SEGMENT"}, "a source without values is marked"
    assert {rule: s.emitted for rule, s in owner.results.items()} == emitted, "no change starts from a baseline"
    assert {pid: p.obs for pid, p in owner.points.items()} == held
    assert group not in recorder.pubs, "never shown as live data"


# ------------------------------------------------------------------------------------------- when it is taken


def test_the_first_group_of_the_bucket_in_a_database_brings_one_baseline_at_the_next_boundary():
    owner, recorder = running_owner()
    some = recorder.commits[-1]
    owner.committed(some, committed_to("primary"))
    owner.committed(some, committed_to("primary"))  # every group before the baseline says so: one marker
    assert baselines(recorder) == [], "the route only marks; nothing is taken until the next batch boundary"
    owner.tick(WALL0 + 10, MONO0 + 10)
    (baseline,) = baselines(recorder)
    owner.committed(some, committed_to("primary"))
    owner.tick(WALL0 + 11, MONO0 + 11)
    assert len(baselines(recorder)) == 1, "one in flight at a time"
    owner.committed(baseline, committed_to("primary", lacks=False))
    owner.tick(WALL0 + 200, MONO0 + 200)
    assert len(baselines(recorder)) == 1, "stored where it was wanted: done"


def test_a_baseline_that_landed_in_the_other_database_is_taken_again_for_the_one_that_still_lacks_it():
    owner, recorder = running_owner()
    owner.committed(recorder.commits[-1], committed_to("primary"))
    owner.tick(WALL0 + 10, MONO0 + 10)
    (first,) = baselines(recorder)
    owner.committed(first, committed_to("emergency", lacks=False))  # the route changed on its way
    owner.tick(WALL0 + 30, MONO0 + 30)
    assert len(baselines(recorder)) == 1, "a fixed pace, not every second"
    owner.tick(WALL0 + 71, MONO0 + 71)
    assert len(baselines(recorder)) == 2, "the primary still lacks one: tried again without waiting for a route change"


def test_a_refused_baseline_is_tried_again_and_turns_nothing_into_a_gap():
    owner, recorder = running_owner()
    owner.committed(recorder.commits[-1], committed_to("primary"))
    owner.tick(WALL0 + 10, MONO0 + 10)
    (first,) = baselines(recorder)
    owner.refused(first, "EMERGENCY_RESERVE")
    assert {s: owner.sources[s].archive for s in ("mb", "ua")} == {"mb": "OK", "ua": "OK"}
    assert owner.health["archive_lost_groups"] == 0
    owner.tick(WALL0 + 71, MONO0 + 71)
    assert len(baselines(recorder)) == 2


def test_a_late_notice_for_a_bucket_already_left_takes_no_new_baseline_for_it():
    clock = Clock(1)
    owner, recorder = running_owner(clock)
    old = replace(recorder.commits[-1], bucket=1)
    clock.no = 2
    owner.tick(WALL0 + 9, MONO0 + 9)
    owner.committed(old, committed_to("primary"))
    owner.tick(WALL0 + 10, MONO0 + 10)
    assert baselines(recorder) == [], "a sealed bucket gets no new baseline"


# ------------------------------------------------------------------------------------------- what the database says


@pytest.fixture
def router(archive_db_factory):
    cfg = config()
    router = ArchiveRouter(cfg, archive_db_factory("primary"), archive_db_factory("emergency"), disk_usage=Disks())
    router.initialize()
    router.bind_run("run1", cfg.config_id, 999.0)
    return router, cfg


def baseline_group(cfg, order, wall):
    state = r.StateRecord(r.state_id("run1", BASELINE_SOURCE, order), "run1", BASELINE_SOURCE, 0, "RETENTION_BASELINE",
                          wall, 50.0, {"set": order, "sources": {}})  # fmt: skip
    return make_group(order=order, config_id=cfg.config_id, kind=r.GROUP_BASELINE, source_id=BASELINE_SOURCE,
                      observations=(), results=(), states=(state,), faults=())  # fmt: skip


def test_the_database_says_whether_the_bucket_has_its_baseline_and_keeps_the_earliest_boundary(router):
    router, cfg = router
    assert router.write_group(make_group(order=1, config_id=cfg.config_id)).lacks_baseline is True
    assert router.write_group(baseline_group(cfg, 2, 1500.0)).lacks_baseline is False
    assert router.write_group(make_group(order=3, config_id=cfg.config_id)).lacks_baseline is False
    router.write_group(baseline_group(cfg, 4, 1800.0))
    with router.primary.session() as conn:
        assert conn.execute("SELECT baseline_wall FROM bucket WHERE no=1").fetchone()["baseline_wall"] == 1500.0
    with router.emergency.session() as conn:
        assert conn.execute("SELECT baseline_wall FROM bucket WHERE no=1").fetchone()["baseline_wall"] is None

