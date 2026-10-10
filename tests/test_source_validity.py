"""D17-D18: source validity, communication alarms, rebuild trigger and same-generation recovery
(终版 §11).  Fake monotonic time, no sleeping: every boundary is exercised exactly.

Why it matters: the 3 s validity, the 6 s rebuild/alarm and the 3-6 s recovery window are only safe
if their baselines are the right ones and the recovery is never published in pieces.
"""

import pytest
from owner_support import Recorder, notify, small_config

from htsg import records as r
from htsg.config import Timing
from htsg.owner import Owner
from htsg.recovery import Backoff

WALL0, MONO0 = 1000.0, 100.0


def ua_only():
    """small_config with the Modbus source switched off, so these tests watch the OPC UA source alone."""
    config = small_config()
    payload = config.model_dump(mode="json")
    payload["sources"][0]["enabled"] = False
    return type(config).model_validate(payload)


def ua_owner(config=None):
    rec = Recorder()
    owner = Owner(config or ua_only(), WALL0, MONO0, commit=rec.commit, publish=rec.publish, run_id="run")
    rebuilds = []
    owner.on_rebuild = lambda source, reason: rebuilds.append((source, reason))
    owner.begin_generation("ua", 1, WALL0, MONO0)
    return owner, rec, rebuilds


def at(mono):
    return WALL0 + (mono - MONO0), mono


def tick(owner, mono):
    owner.tick(*at(mono))


def data(owner, mono, items=(("ti", 500.0), ("to", 450.0)), gen=1, seq=1):
    wall, mono = at(mono)
    return owner.accept(notify(items, gen=gen, wall=wall, mono=mono, seq=seq))


def keepalive(owner, mono, gen=1):
    wall, mono = at(mono)
    return owner.accept(r.InputBatch("ua", gen, r.KIND_UA_NOTIFY, wall, mono))


# ---------------------------------------------------------------------------- D17


def test_data_expires_exactly_three_seconds_after_the_last_valid_response_without_a_rebuild():
    owner, rec, rebuilds = ua_owner()
    data(owner, 101.0)
    keepalive(owner, 102.0)
    tick(owner, 104.9)
    assert owner.sources["ua"].state == "VALID"
    tick(owner, 105.0)
    s = owner.sources["ua"]
    assert s.state == "EXPIRED" and rebuilds == [], "expiry alone never rebuilds"
    end = rec.states("SEG_END")[-1]
    assert end.detail["reason"] == "EXPIRED" and end.detail["end_wall"] == at(105.0)[0]
    assert owner.points["ti"].obs.value == 500.0, "the cache is kept ..."
    assert owner.results["dt"].current.reason == "SOURCE_INVALID", "... but no longer counts"
    assert owner._input("ti").valid is False


def test_keepalives_prove_activity_so_a_quiet_but_healthy_source_never_expires():
    owner, rec, rebuilds = ua_owner()
    data(owner, 101.0)
    for mono in range(102, 140, 2):  # one keep-alive every 2 s, no value change at all
        keepalive(owner, float(mono))
        tick(owner, mono + 0.5)
    assert owner.sources["ua"].state == "VALID" and not rebuilds and not owner.sources["ua"].alarm


def test_a_stationary_point_is_never_aged_out_by_time():
    owner, rec, rebuilds = ua_owner()
    data(owner, 101.0)
    for mono in range(102, 3700, 2):
        keepalive(owner, float(mono))
        tick(owner, float(mono))
    assert owner._input("ti").valid and owner.results["dt"].current.value == 50.0


def test_an_explicit_disconnect_invalidates_at_once_and_asks_for_a_rebuild():
    owner, rec, rebuilds = ua_owner()
    data(owner, 101.0)
    owner.source_down("ua", 1, "CONNECTION", *at(101.2))
    assert owner.sources["ua"].state == "RESYNC" and rebuilds == [("ua", "DOWN:CONNECTION")]
    assert owner.results["dt"].current.reason == "SOURCE_INVALID"
    owner.source_down("ua", 1, "CONNECTION", *at(101.3))
    assert len(rebuilds) == 1, "a repeated report is idempotent"
    owner.source_down("ua", 0, "OLD", *at(101.4))  # a stale generation cannot affect the current one


def test_six_seconds_of_silence_raise_the_alarm_and_request_a_rebuild_together():
    owner, rec, rebuilds = ua_owner()
    data(owner, 101.0)
    keepalive(owner, 102.0)
    tick(owner, 107.9)
    assert owner.sources["ua"].state == "EXPIRED" and not owner.sources["ua"].alarm and rebuilds == []
    tick(owner, 108.0)
    assert owner.sources["ua"].state == "RESYNC" and rebuilds == [("ua", "SILENCE")]
    assert owner.sources["ua"].alarm
    alarm = rec.states("COMM_ALARM")[0]
    assert alarm.detail["never_responded"] is False and alarm.detail["silent_seconds"] == pytest.approx(6.0)


def test_before_any_response_the_alarm_runs_from_the_moment_the_source_was_enabled():
    owner, rec, rebuilds = ua_owner()
    tick(owner, 105.9)
    assert not owner.sources["ua"].alarm
    tick(owner, 106.0)
    assert owner.sources["ua"].alarm and rec.states("COMM_ALARM")[0].detail["never_responded"] is True
    assert owner.points["ti"].obs is None and owner.results["dt"].emitted is None, "data stays unknown"
    assert rec.results() == [] and rec.observations() == []


def test_rebuild_attempts_do_not_reset_the_alarm_baseline_only_a_valid_response_does():
    owner, rec, rebuilds = ua_owner()
    data(owner, 101.0)
    tick(owner, 107.0)
    assert owner.sources["ua"].alarm and owner.sources["ua"].state == "RESYNC"
    owner.begin_generation("ua", 2, *at(108.0))  # a new subscription starts waiting
    tick(owner, 111.0)
    tick(owner, 113.5)
    assert owner.sources["ua"].alarm and len(rec.states("COMM_ALARM")) == 1, "not cleared, not raised again"
    assert owner.sources["ua"].last_comm_mono == at(101.0)[1], "no response, so the communication time stands"
    data(owner, 113.6, gen=2)
    assert not owner.sources["ua"].alarm and len(rec.states("COMM_RESTORED")) == 1


def test_a_source_disabled_by_configuration_never_accumulates_a_communication_alarm():
    config = small_config()
    payload = config.model_dump(mode="json")
    payload["sources"][0]["enabled"] = False
    payload["sources"][1]["enabled"] = False  # both protocols switched off by configuration
    owner = Owner(type(config).model_validate(payload), WALL0, MONO0, run_id="run")
    for mono in range(101, 2000):
        owner.tick(*at(float(mono)))
    assert not owner.sources["ua"].alarm and not owner.sources["mb"].alarm and owner.health["resyncs"] == 0


def test_backoff_doubles_to_the_cap_and_resets_only_after_sixty_stable_seconds():
    b = Backoff(Timing())
    assert [b.next_delay() for _ in range(7)] == [1, 2, 4, 8, 16, 30, 30]
    b.healthy(1000.0)
    assert not b.settle(1059.9) and b.failures == 7
    assert b.settle(1060.0) and b.failures == 0 and b.next_delay() == 1


def test_a_short_healthy_spell_followed_by_failure_does_not_reset_the_backoff():
    b = Backoff(Timing())
    b.next_delay(), b.next_delay()
    b.healthy(0.0)
    assert b.next_delay() == 4, "failed after 30 s of health: not yet stable, the schedule continues"
    assert b.healthy_since is None and not b.settle(100.0)


def test_five_consecutive_failures_flag_persistent_resync_but_recovery_keeps_going_every_30_s():
    b = Backoff(Timing())
    for _ in range(4):
        b.next_delay()
    assert not b.persistent
    b.next_delay()
    assert b.persistent
    assert [b.next_delay() for _ in range(3)] == [30, 30, 30], "no manual unlock, same 30 s cadence"
    b.healthy(0.0)
    assert b.settle(60.0) and not b.persistent


# ---------------------------------------------------------------------------- D18


def expired_owner():
    owner, rec, rebuilds = ua_owner()
    data(owner, 101.0)
    tick(owner, 105.0)  # last response at 101.0: expired 3 s later
    assert owner.sources["ua"].state == "EXPIRED"
    return owner, rec, rebuilds


def test_recovery_inside_three_to_six_seconds_keeps_the_generation_and_commits_one_group():
    owner, rec, rebuilds = expired_owner()
    groups_before, pubs_before = len(rec.commits), len(rec.pubs)
    data(owner, 105.5, items=(("ti", 501.0),), seq=2)
    s = owner.sources["ua"]
    assert s.state == "VALID" and s.conn_gen == 1 and s.seg == 2 and rebuilds == [], "no rebuild"
    new = rec.commits[groups_before:]
    assert [g.kind for g in new] == [r.GROUP_RECOVERY], "baseline + recovery batch + results: ONE group"
    assert len(rec.pubs) == pubs_before + 1, "and published as one unit, never the old cache alone"
    group = new[0]
    assertions = [o for o in group.observations if o.origin == r.ORIGIN_ASSERTION]
    received = [o for o in group.observations if o.origin == r.ORIGIN_RECEIVED]
    assert {o.point_id for o in assertions} == {"ti", "to"} and [o.value for o in received] == [501.0]
    old = {o.point_id: o for o in rec.observations() if o.origin == r.ORIGIN_RECEIVED and o.batch_seq == 1}
    for a in assertions:
        assert a.orig_id == old[a.point_id].id and a.received_wall == old[a.point_id].received_wall
        assert a.asserted_wall == at(105.5)[0] and a.seg == 2
    result_values = {x.point_id: (x.value, x.origin) for x in group.results}
    assert ("dt" in result_values) and 50.0 in [x.value for x in group.results if x.origin == r.ORIGIN_ASSERTION]
    assert owner.results["dt"].current.value == pytest.approx(51.0), "updated from the final valid state"
    assert [x.kind for x in group.states] == ["SEG_START"] and group.states[0].detail["reason"] == "RECOVERY"


def test_recovery_by_a_keepalive_alone_restates_the_cache_and_changes_nothing_else():
    owner, rec, rebuilds = expired_owner()
    seq_before = owner.sources["ua"].batch_seq
    keepalive(owner, 105.5)
    group = rec.commits[-1]
    assert group.kind == r.GROUP_RECOVERY and owner.sources["ua"].batch_seq == seq_before
    assert all(o.origin == r.ORIGIN_ASSERTION for o in group.observations)
    assert owner.sources["ua"].state == "VALID" and owner.results["dt"].current.value == 50.0
    assert group.results and all(x.origin == r.ORIGIN_ASSERTION for x in group.results), (
        "unchanged results are re-stated, never rewritten as new values"
    )


def test_the_expired_interval_is_not_backfilled_by_the_recovery():
    owner, rec, _ = expired_owner()
    keepalive(owner, 105.5)
    start = rec.states("SEG_START")[-1].at_wall
    end = [s for s in rec.states("SEG_END") if s.detail["reason"] == "EXPIRED"][0].detail["end_wall"]
    assert end == at(104.0)[0] + 0.0 or end <= start
    effective = [o.asserted_wall or o.received_wall for g in rec.commits[-1:] for o in g.observations]
    assert min(effective) >= at(105.5)[0], "re-stated values count from the recovery moment, not earlier"


def test_old_generation_responses_cannot_revive_the_old_cache_after_a_rebuild():
    owner, rec, rebuilds = ua_owner()
    data(owner, 101.0)
    tick(owner, 107.0)
    assert owner.sources["ua"].state == "RESYNC"
    assert data(owner, 107.5, items=(("ti", 999.0),), gen=1) is False
    owner.begin_generation("ua", 2, *at(108.0))
    assert owner.points["ti"].obs is None and owner.results["dt"].current is None, "nothing carried over"
    assert data(owner, 108.2, items=(("ti", 510.0),), gen=1, seq=9) is False, "late old-generation batch"
    data(owner, 108.5, items=(("ti", 510.0),), gen=2)
    assert owner.results["dt"].current.reason == "INPUT_UNAVAILABLE", "'to' has no initial value yet: unknown"
    assert 999.0 not in [o.value for o in rec.observations()]


def test_new_subscription_wait_boundaries_are_independent_of_the_source_alarm_baseline():
    owner, rec, rebuilds = ua_owner()
    data(owner, 101.0)
    tick(owner, 107.0)  # old generation: resync + alarm raised (6 s after 101.0)
    assert owner.sources["ua"].alarm
    owner.begin_generation("ua", 2, *at(120.0))  # a NEW wait starts at 120
    rebuilds.clear()
    tick(owner, 122.9)
    assert owner.sources["ua"].state == "WAITING" and not rebuilds
    tick(owner, 123.5)  # 3.5 s into the wait: data are still unknown, nothing expires or rebuilds
    assert owner.sources["ua"].state == "WAITING" and not rebuilds and owner._input("ti").valid is False
    tick(owner, 125.9)
    assert not rebuilds
    tick(owner, 126.0)
    assert rebuilds == [("ua", "SILENCE")], "6 s counted from the subscription's own start (120), not from 101"


def test_first_data_in_a_waiting_subscription_makes_it_valid_without_a_recovery_segment():
    owner, rec, rebuilds = ua_owner()
    owner.begin_generation("ua", 2, *at(120.0))
    segs_before = owner.sources["ua"].seg
    data(owner, 124.0, gen=2)  # 4 s into the wait
    s = owner.sources["ua"]
    assert s.state == "VALID" and s.seg == segs_before and rec.commits[-1].kind == r.GROUP_BATCH
    assert owner.results["dt"].current.value == 50.0 and rebuilds == []
