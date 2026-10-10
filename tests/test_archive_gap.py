"""D24-D25 (owner side): archive loss becomes a gap, and recovery is one reliable unit (终版 §5 C2).

Why it matters: after lost records the history must say "unknown here", and the way back must not
certify a stale cache, rewrite a sample, or pile up memory without limit.
"""

import pytest
from owner_support import Recorder, poll, small_config
from test_source_validity import MONO0, WALL0, at, data, ua_only

from htsg import records as r
from htsg.owner import Owner


class Sink:
    """A commit sink that can refuse (a full queue) and remembers what it accepted."""

    def __init__(self):
        self.accept = True
        self.groups = []
        self.offered = 0

    def __call__(self, group):
        self.offered += 1
        if not self.accept:
            return False
        self.groups.append(group)
        return True

    def kinds(self):
        return [g.kind for g in self.groups]


def build(config=None):
    sink, rec = Sink(), Recorder()
    owner = Owner(config or ua_only(), WALL0, MONO0, commit=sink, publish=rec.publish, run_id="run")
    owner.begin_generation("ua", 1, WALL0, MONO0)
    return owner, sink, rec


def lose_the_archive(owner, sink, mono=102.0, items=(("ti", 501.0),)):
    sink.accept = False
    data(owner, mono, items=items, seq=2)
    sink.accept = True


def test_a_refused_offer_opens_a_gap_and_nothing_of_that_source_is_queued_across_it():
    owner, sink, rec = build()
    data(owner, 101.0)
    queued = len(sink.groups)
    sink.accept = False
    data(owner, 102.0, items=(("ti", 501.0),), seq=2)
    s = owner.sources["ua"]
    assert s.archive == "LOST" and s.lost_reason == "QUEUE_FULL" and s.lost_since_wall == at(102.0)[0]
    sink.accept = True
    offered_before = sink.offered
    pubs_before = len(rec.pubs)
    data(owner, 103.0, items=(("ti", 502.0),), seq=3)
    assert sink.offered == offered_before, "nothing is queued for this source while the gap is open"
    assert len(rec.pubs) == pubs_before + 1, "realtime delivery is unaffected by the archive"
    assert len(sink.groups) == queued and owner.health["archive_lost_groups"] == 2


def test_recovery_commits_gap_new_segment_baseline_and_first_watermark_as_one_group():
    owner, sink, rec = build()
    data(owner, 101.0)
    lose_the_archive(owner, sink)
    data(owner, 103.0, items=(("ti", 502.0),), seq=3)  # more traffic during the gap
    pubs_before = len(rec.pubs)
    owner.tick(*at(104.0))
    recovery = sink.groups[-1]
    assert recovery.kind == r.GROUP_RECOVERY and owner.sources["ua"].archive == "RECOVERING"
    kinds = [s.kind for s in recovery.states]
    assert kinds == ["GAP", "SEG_END", "SEG_START", "WATERMARK"]
    gap, end, start, mark = recovery.states
    assert (gap.detail["start_wall"], gap.detail["end_wall"], gap.detail["reason"]) == (
        at(102.0)[0], at(104.0)[0], "QUEUE_FULL",
    )  # fmt: skip
    assert end.seg == 1 and end.detail["end_wall"] == at(102.0)[0], "the old interval ends where the loss began"
    assert start.seg == 2 and start.detail["reason"] == "ARCHIVE_GAP"
    assert mark.detail["seg"] == 2
    values = {o.point_id: o.value for o in recovery.observations}
    assert values == {"ti": 502.0, "to": 450.0}, "the baseline is the CURRENT retained state, taken now"
    assert len(rec.pubs) == pubs_before, "an archive-only unit is not republished to clients"


def test_the_baseline_keeps_original_identity_and_flags_values_derived_inside_the_gap():
    owner, sink, _ = build()
    data(owner, 101.0)
    ids_before = {o.point_id: o.id for g in sink.groups for o in g.observations}
    lose_the_archive(owner, sink)  # ti 501.0 lost; dt becomes 51.0 inside the gap
    owner.tick(*at(104.0))
    recovery = sink.groups[-1]
    for o in recovery.observations:
        assert o.origin == r.ORIGIN_ASSERTION and o.asserted_wall == at(104.0)[0]
    by_point = {o.point_id: o for o in recovery.observations}
    assert by_point["to"].orig_id == ids_before["to"] and by_point["to"].received_wall == at(101.0)[0]
    dt = next(x for x in recovery.results if x.point_id == "dt")
    assert dt.origin == r.ORIGIN_ASSERTION and dt.value == 51.0
    assert dt.basis_incomplete is True, "its inputs were lost in the gap: not a recovered calculation"


def test_values_derived_before_the_gap_stay_complete():
    owner, sink, _ = build()
    data(owner, 101.0)
    sink.accept = False
    data(owner, 102.0, items=(("ti", 500.0),), seq=2)  # lost, but nothing changes: dt stays the pre-gap result
    sink.accept = True
    owner.tick(*at(104.0))
    dt = next(x for x in sink.groups[-1].results if x.point_id == "dt")
    assert dt.value == 50.0 and dt.basis_incomplete is False


def test_an_out_of_sync_intake_is_fixed_by_a_rebuild_first_and_the_stale_cache_is_no_baseline():
    owner, sink, _ = build()
    data(owner, 101.0)
    lose_the_archive(owner, sink)
    owner.source_down("ua", 1, "CONNECTION", *at(103.0))
    groups = len(sink.groups)
    owner.tick(*at(104.0))
    assert len(sink.groups) == groups and owner.sources["ua"].archive == "LOST", "no recovery from old state"
    owner.begin_generation("ua", 2, *at(105.0))
    data(owner, 105.5, items=(("ti", 520.0), ("to", 460.0)), gen=2, seq=1)
    owner.tick(*at(106.0))
    recovery = sink.groups[-1]
    assert recovery.kind == r.GROUP_RECOVERY
    assert {o.point_id: o.value for o in recovery.observations} == {"ti": 520.0, "to": 460.0}
    assert all(o.conn_gen == 2 for o in recovery.observations), "nothing of the old generation leaks in"
    assert [s.detail.get("conn_gen") for s in recovery.states if s.kind == "SEG_START"] == [2]


def test_the_restatement_computes_nothing_and_cuts_the_change_chain():
    sink, rec = Sink(), Recorder()
    owner = Owner(small_config(), WALL0, MONO0, commit=sink, publish=rec.publish, run_id="run")
    for source in ("mb", "ua"):
        owner.begin_generation(source, 1, WALL0, MONO0)
    owner.accept(poll(1, {"b1": 0.6, "b2": 0.6}))
    owner.accept(poll(2, {"b1": 0.9, "b2": 0.6}))
    assert owner.results["c1"].current.value == pytest.approx(50.0)
    sink.accept = False
    owner.accept(poll(3, {"b1": 0.9, "b2": 0.6}))
    sink.accept = True
    owner.tick(1004.0, 104.0)
    recovery = sink.groups[-1]
    assert recovery.kind == r.GROUP_RECOVERY and recovery.source_id == "mb"
    assert not recovery.faults and all(x.origin == r.ORIGIN_ASSERTION for x in recovery.results)
    assert all(o.origin == r.ORIGIN_ASSERTION for o in recovery.observations)
    owner.accept(poll(4, {"b1": 1.2, "b2": 0.6}))
    assert owner.results["c1"].current.reason == "BASELINE_ONLY", "no difference may span the archive gap"
