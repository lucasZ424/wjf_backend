"""D29-D31: faults inside the owner - 304 channels, batch-internal versions, identity, buffering
(终版 §8, §8.1, §8.2, §12).

Why it matters: the fault list is the reference point of history replay.  A timer that survives a changed
batch, a record that mixes confirmation time with sample time, or a buffer that silently drops, would each
make that list untrustworthy.
"""

from dataclasses import replace

from fault_support import Accepting, at, fault_config, fault_owner, faults_of, feed, keepalive, tick

from htsg import records as r
from htsg.archive_router import group_bytes
from htsg.config import load_settings
from htsg.owner import Owner


def run_ticks(owner, start, stop, alive=True):
    """One tick per second.  A live subscription also delivers keep-alives, so by default the source stays valid."""
    mono = start
    while mono <= stop + 1e-9:
        if alive:
            keepalive(owner, mono)
        tick(owner, mono)
        mono += 1.0


# ------------------------------------------------------------------- D29


def test_all_304_channels_are_covered_including_96_differences_and_8_strains():
    research = load_settings("config/research-sim.json")
    owner = Owner(research, 1000.0, 100.0, run_id="x")
    assert len(owner.rules) == 304
    assert sum(1 for k in owner.rules if k.startswith("delta_t_")) == 96
    assert sum(1 for k in owner.rules if k.startswith("strain_") and not k.startswith("strain_change")) == 8
    assert not any(k.startswith(("bridge_", "strain_change_", "stress_", "thermal_stress")) for k in owner.rules)


def test_a_differently_valued_second_version_in_one_batch_restarts_the_derived_timer():
    # spec §8.2: outer 450, inner 490 -> 530 in ONE batch; the batch-end difference is 80, but the clock
    # of the previous batch (60 > 55 since 100.0) must not carry over
    owner, sink, _ = fault_owner(fault_config(point_rules=False))
    feed(owner, 100.0, [("ti", 510.0), ("to", 450.0)])  # dt = 60: pending since 100.0
    tick(owner, 101.0)
    feed(owner, 103.0, [("ti", 490.0), ("ti", 530.0)], seq=2)  # two versions of one source point
    run_ticks(owner, 104.0, 107.0)
    assert faults_of(sink) == [], "5 s from 100.0 would be 105.0: the old clock must be gone"
    tick(owner, 108.0)
    (fault,) = faults_of(sink)
    assert fault.point_id == "dt" and fault.basis_value == 80.0, "restarted at 103.0, confirmed 5 s later"


def test_a_single_version_update_keeps_the_running_clock():
    owner, sink, _ = fault_owner(fault_config(point_rules=False))
    feed(owner, 100.0, [("ti", 510.0), ("to", 450.0)])
    feed(owner, 103.0, [("ti", 530.0)], seq=2)  # same direction, one version: the clock keeps running
    run_ticks(owner, 104.0, 105.0)
    assert [f.point_id for f in faults_of(sink)] == ["dt"]


def test_identical_repeats_and_unrelated_updates_do_not_reset_the_timer():
    cfg = fault_config(point_rules=False)
    payload = cfg.model_dump(mode="json")
    payload["points"].append({"id": "x", "source": "ua", "unit": "u", "node_id": "ns=2;s=x"})
    owner, sink, _ = fault_owner(type(cfg).model_validate(payload))
    feed(owner, 100.0, [("ti", 510.0), ("to", 450.0)])
    feed(owner, 102.0, [("ti", 510.0), ("ti", 510.0)], seq=2)  # exact repeat: not a new version
    feed(owner, 103.0, [("x", 1.0)], seq=3)  # unrelated point
    run_ticks(owner, 104.0, 105.0)
    assert [f.point_id for f in faults_of(sink)] == ["dt"], "confirmed 5 s after the ORIGINAL start"


def test_two_inputs_updating_once_each_is_not_a_multi_version_batch():
    owner, sink, _ = fault_owner(fault_config(point_rules=False))
    feed(owner, 100.0, [("ti", 510.0), ("to", 450.0)])
    feed(owner, 102.0, [("ti", 512.0), ("to", 451.0)], seq=2)  # still 61 > 55, one version each
    run_ticks(owner, 103.0, 105.0)
    assert len(faults_of(sink)) == 1


def test_only_the_batch_end_result_decides_recovery_of_a_confirmed_fault():
    owner, sink, _ = fault_owner(fault_config(point_rules=False))
    feed(owner, 100.0, [("ti", 510.0), ("to", 450.0)])
    run_ticks(owner, 101.0, 105.0)
    assert owner.tracker.confirmed_direction("dt") == "HIGH"
    feed(owner, 106.0, [("ti", 500.0), ("ti", 520.0)], seq=2)  # passes through 50 but ends at 70
    assert owner.tracker.confirmed_direction("dt") == "HIGH", "an intermediate normal value does not recover it"
    feed(owner, 107.0, [("ti", 520.0), ("ti", 500.0)], seq=3)  # ends at 50 <= 55 - 4
    assert owner.tracker.confirmed_direction("dt") is None
    assert len(faults_of(sink)) == 1, "recovery is state, not a second record"


def test_every_valid_normal_source_observation_cancels_a_pending_source_clock_in_batch_order():
    owner, sink, _ = fault_owner(fault_config(derived_rules=False))
    feed(owner, 100.0, [("ti", 495.0), ("ti", 480.0), ("ti", 495.0)])  # violate, normal, violate: restarts at 100.0
    run_ticks(owner, 101.0, 104.0)
    assert faults_of(sink) == []
    tick(owner, 105.0)
    assert [f.point_id for f in faults_of(sink)] == ["ti"]


def test_communication_failure_is_judged_before_the_clock_advances_in_the_same_tick():
    owner, sink, _ = fault_owner(fault_config(point_rules=False))
    feed(owner, 100.0, [("ti", 510.0), ("to", 450.0)])  # pending since 100.0 -> would confirm at 105.0
    keepalive(owner, 102.0)  # last valid response at 102.0 -> the data expire at 105.0, the same tick
    run_ticks(owner, 103.0, 104.0, alive=False)
    assert owner.sources["ua"].state == "VALID"
    tick(owner, 105.0)
    assert owner.sources["ua"].state == "EXPIRED"
    assert faults_of(sink) == [], "communication loss cancelled the clock before it could confirm"


def test_recovery_after_a_short_outage_restarts_the_clock_from_the_final_valid_state():
    owner, sink, _ = fault_owner(fault_config(point_rules=False))
    feed(owner, 100.0, [("ti", 510.0), ("to", 450.0)])
    run_ticks(owner, 101.0, 103.0, alive=False)  # 3.0 s after the last response: expired, clock cancelled
    assert owner.sources["ua"].state == "EXPIRED"
    keepalive(owner, 104.5)  # same subscription resumes: the retained 510/450 count again, from NOW
    assert owner.sources["ua"].state == "VALID" and owner.tracker.status("dt") == "PENDING"
    run_ticks(owner, 105.0, 109.0)
    assert faults_of(sink) == [], "a restated cache never confirms on the strength of the old clock"
    tick(owner, 109.5)
    assert len(faults_of(sink)) == 1


def test_bad_quality_and_extreme_values_neither_start_nor_confirm_faults():
    owner, sink, _ = fault_owner(fault_config(derived_rules=False))
    feed(owner, 100.0, [("ti", 600.0, r.BAD, 0x80000000), ("to", 900.0)])  # bad; 900 is beyond the 800 extreme
    run_ticks(owner, 101.0, 120.0)
    keepalive_times = range(102, 121, 2)
    for t in keepalive_times:
        keepalive(owner, float(t))
    assert faults_of(sink) == []


# ------------------------------------------------------------------- D30


def confirm_one(owner, sink, value=510.0):
    feed(owner, 100.4, [("ti", value), ("to", 450.0)])
    run_ticks(owner, 101.0, 106.0)
    return faults_of(sink)[0]


def test_confirmation_time_and_basis_observation_time_are_recorded_separately():
    owner, sink, _ = fault_owner(fault_config(point_rules=False))
    fault = confirm_one(owner, sink)
    assert fault.point_id == "dt" and fault.direction == "HIGH"
    assert fault.basis_wall == at(100.4)[0], "the time the deciding value was observed"
    assert fault.confirmed_wall == at(106.0)[0], "the tick that completed 5 s of valid state, not 'a sample'"
    assert fault.confirmed_wall - fault.basis_wall > 5 and fault.basis_value == 60.0
    source_fault = fault_owner(fault_config(derived_rules=False))
    f2 = confirm_one(*source_fault[:2], value=495.0)
    assert f2.basis_kind == "OBSERVATION" and f2.basis_id.endswith("/b1/i0") and f2.basis_wall == at(100.4)[0]


def test_the_record_carries_the_threshold_snapshot_configuration_version_and_lineage():
    owner, sink, _ = fault_owner(fault_config(point_rules=False))
    fault = confirm_one(owner, sink)
    assert fault.rule == {"low": -55.0, "high": 55.0, "hysteresis_low": 4.0, "hysteresis_high": 4.0}
    assert fault.config_id == owner.config.config_id
    assert (fault.run_id, fault.source_id, fault.seg, fault.batch_seq) == ("run", "ua", 1, 1)
    assert fault.basis_kind == "RESULT" and fault.basis_id == "run/ua/b1/r/dt"


def test_a_timer_generated_fault_travels_the_same_ordered_path_as_its_source():
    owner, sink, rec = fault_owner(fault_config(point_rules=False))
    confirm_one(owner, sink)
    group = next(g for g in sink.groups if g.faults)
    assert group.kind == r.GROUP_FAULT and group.source_id == "ua"
    assert group.order > next(g.order for g in sink.groups if g.observations), "after the data it rests on"
    (fault,) = group.faults
    assert owner.channel_views()[fault.point_id]["fault_direction"] == fault.direction, "and the live view shows it at once"


def test_resubmitting_a_confirmed_fault_keeps_its_id(archive_db_factory):
    owner, sink, _ = fault_owner(fault_config(point_rules=False))
    fault = confirm_one(owner, sink)
    store = archive_db_factory()
    store.initialize(owner.config)
    store.bind_run("run", owner.config.config_id, 1000.0)
    for group in sink.groups:
        assert store.write_group(group).durable
    again = owner._build(r.GROUP_RECOVERY, "ua", 1200.0, states=[], faults=[fault])
    assert store.write_group(again).durable
    with store.session() as conn:
        assert conn.execute("SELECT count(*) AS n FROM fault_record").fetchone()["n"] == 1
    assert fault.id == r.fault_id("run", "dt", 1)


# ------------------------------------------------------------------- D31


def test_faults_confirmed_during_an_archive_outage_stay_buffered_and_ride_the_recovery_unit():
    owner, sink, _ = fault_owner(fault_config(derived_rules=False))
    feed(owner, 100.0, [("ti", 450.0), ("to", 450.0)])
    sink.accept = False
    feed(owner, 101.0, [("ti", 495.0), ("to", 495.0)], seq=2)  # lost group: the archive is out
    run_ticks(owner, 102.0, 106.0)
    assert owner.fault_status()["buffered"] == 2 and faults_of(sink) == []
    sink.accept = True
    tick(owner, 107.0)
    recovery = sink.groups[-1]
    assert recovery.kind == r.GROUP_RECOVERY and {f.point_id for f in recovery.faults} == {"ti", "to"}
    owner.refused(recovery, "WRITE_BATCH_BUDGET")
    assert owner.fault_status()["buffered"] == 2, "removed only when a commit succeeded"
    tick(owner, 108.0)
    again = sink.groups[-1]
    owner.committed(again)
    assert owner.fault_status()["buffered"] == 0


def test_the_buffer_holds_1000_refuses_the_1001st_and_keeps_a_countable_summary():
    owner, sink, _ = fault_owner(fault_config(point_rules=False), sink=Accepting())
    sink.accept = False
    feed(owner, 100.0, [("ti", 450.0), ("to", 450.0)])  # archive out from the first group on
    mono, seq = 101.0, 2
    cycles = 0
    while owner.fault_status()["buffered"] < 1000:
        feed(owner, mono, [("ti", 520.0)], seq=seq)  # dt = 70: violate
        for _ in range(6):
            mono += 1.0
            tick(owner, mono)
            keepalive(owner, mono)
        feed(owner, mono + 0.1, [("ti", 450.0)], seq=seq + 1)  # dt = 0: recovers
        mono, seq, cycles = mono + 1.0, seq + 2, cycles + 1
        assert cycles < 1100
    assert owner.fault_status()["buffered"] == 1000
    first_kept = sorted(owner.fault_buffer)[0]
    for _ in range(3):  # three more confirmations: refused, never overwriting
        feed(owner, mono, [("ti", 520.0)], seq=seq)
        for _ in range(6):
            mono += 1.0
            tick(owner, mono)
            keepalive(owner, mono)
        feed(owner, mono + 0.1, [("ti", 450.0)], seq=seq + 1)
        mono, seq = mono + 1.0, seq + 2
    status = owner.fault_status()
    assert status["buffered"] == 1000 and first_kept in owner.fault_buffer
    assert status["archive_incomplete"] is True and status["rejected_total"] == 3
    summary = status["loss"]["ua"]
    assert summary["count"] == 3 and summary["first_wall"] < summary["last_wall"]
    assert owner.health["fault_archive_incomplete"] is True


def test_the_recovery_unit_with_1000_faults_and_a_full_baseline_fits_the_group_budget():
    research = load_settings("config/research-sim.json")
    payload = research.model_dump(mode="json")
    payload["simulation"]["enabled"] = False
    sink = Accepting()
    owner = Owner(type(research).model_validate(payload), 1000.0, 100.0, commit=sink, run_id="big")
    sink.owner = owner
    for source in ("mb_field", "ua_field"):
        owner.begin_generation(source, 1, 1000.0, 100.0)
    items = tuple(r.Item(p.id, 480.0 + i * 0.01) for i, p in enumerate(research.points) if p.source == "ua_field")
    owner.accept(r.InputBatch("ua_field", 1, r.KIND_UA_NOTIFY, 1001.0, 101.0, items, publish_seq=1))
    base = next(g for g in sink.groups if g.observations)
    fault = r.FaultRecord(
        "x", "big", "ua_field", 1, 1, "temp_inner_01", "HIGH", 1.0, 1.0, "OBSERVATION", base.observations[0].id,
        1.0, 495.0, owner.config.config_id, {"low": None, "high": 490.0, "hysteresis_low": 0.0, "hysteresis_high": 3.0},
    )  # fmt: skip
    s = owner.sources["ua_field"]
    for n in range(1000):
        owner.fault_buffer[f"big/fault/{n}"] = replace(fault, id=f"big/fault/{n}")
    s.archive, s.lost_since_wall, s.lost_reason, s.lost_from_batch_seq = "LOST", 1001.0, "QUEUE_FULL", 1
    owner.tick(1003.0, 103.0)
    recovery = sink.groups[-1]
    assert recovery.kind == r.GROUP_RECOVERY and len(recovery.faults) == 1000
    assert len(recovery.observations) == 200 and len(recovery.results) >= 1
    assert group_bytes(recovery) < owner.config.storage.batch_budget_bytes, group_bytes(recovery)


def test_status_exposes_incomplete_fault_archive_and_losses_for_the_current_page():
    owner, sink, _ = fault_owner(fault_config(point_rules=False))
    status = owner.fault_status()
    assert status["archive_incomplete"] is False and status["rejected_total"] == 0
    assert status["limit"] == 1000 and "IN_MEMORY" in status["durability"], "the in-process loss risk is stated"
