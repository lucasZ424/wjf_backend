"""Capstone B: the frame-free calculation loop at full scale (方案第 4 节).

230 inputs and 25 derived channels driven through the owner with an in-memory commit sink.
An independent oracle recomputes every derived value from the owner's own raw observations after
each batch, so a result mixing old and new inputs fails.  Real protocols and storage are out of scope.
"""

import dataclasses
from pathlib import Path

import pytest
from owner_support import FixedBucket, Recorder, notify, poll

from htsg import records as r
from htsg.config import load_settings
from htsg.owner import Owner

ROOT = Path(__file__).resolve().parents[1]
FULL = load_settings(ROOT / "config" / "research-sim.json")
BRIDGES = [p.id for p in FULL.points if p.id.startswith("bridge_mvv_")]
WALLS = [f"temp_wall_{i:03d}" for i in range(1, 216)]
PROCESS = {
    p.id: 1.0 for p in FULL.points if not p.id.startswith(("bridge_", "temp_"))
}  # seven process values
MPA_PER_MICROSTRAIN = 170000 * 1e-6  # E, 热应力估算模型


def build():
    recorder = Recorder()
    owner = Owner(FULL, 1000.0, 100.0, commit=recorder.commit, buckets=FixedBucket(), run_id="full")
    for source in ("mb_field", "ua_field"):
        owner.begin_generation(source, 1, 1000.0, 100.0)
    return owner, recorder


def mb(tick, values, **kw):
    return poll(tick, dict(zip(BRIDGES, values, strict=True)), source="mb_field", **kw)


def ua(items, wall, seq):
    return notify(items, source="ua_field", wall=wall, mono=wall - 900, seq=seq)


def good(owner, name):
    ps = owner.points[name]
    return ps.obs.value if ps.good and owner.sources[owner.point_source[name]].state == "VALID" else None


def oracle(owner):
    """Expected derived values, from nothing but the owner's current raw observations."""
    expected = {}
    stresses = []
    for i in range(8):
        n = f"{i + 1:02d}"
        b = good(owner, BRIDGES[i])
        expected[f"strain_{n}"] = None if b is None else b * 1000 / 6
        stress = None if b is None else b * 1000 / 6 * MPA_PER_MICROSTRAIN
        expected[f"stress_{n}"] = stress
        stresses.append(stress)
    # 修复轮15: the largest magnitude with its sign, the first of equal magnitudes in channel order
    expected["thermal_stress_peak"] = None if None in stresses else max(stresses, key=abs)
    return expected


def check_consistency(owner):
    for name, value in oracle(owner).items():
        current = owner.results[name].current
        if value is None:
            assert current is None or current.status == r.RESULT_NA, name
        else:
            assert current.status == r.RESULT_OK and current.value == pytest.approx(value), name


def bad_round(tick, values, index, wall):
    """One round in which one bridge answers with a bad quality: the number stays, it is not to be used."""
    items = tuple(
        r.Item(b, v, r.BAD if k == index else r.GOOD, 0x80350000 if k == index else None, response_id="resp0")
        for k, (b, v) in enumerate(zip(BRIDGES, values, strict=True))
    )
    return r.InputBatch("mb_field", 1, r.KIND_MODBUS_POLL, wall, wall - 900, items, tick=tick, poll_id=f"poll{tick}", outcome="OK")


def test_full_scale_loop_matches_an_independent_oracle_through_every_scenario():
    owner, rec = build()
    initial = [*((n, 480.0 + i * 0.01) for i, n in enumerate(WALLS)), *PROCESS.items()]
    assert len(initial) == 222

    # initial burst of all 222 OPC UA points arrives as one batch: nothing is calculated from it (C6)
    owner.accept(ua(initial, 1001.0, 1))
    check_consistency(owner)
    assert rec.results() == []

    # Modbus rounds; round 3 crosses zero legally; the round straddles a second boundary
    values = [0.7 + 0.01 * i for i in range(8)]
    owner.accept(mb(1, values, wall=1001.0))
    check_consistency(owner)
    assert owner.results["thermal_stress_peak"].current.status == r.RESULT_OK
    owner.accept(mb(2, [v + 0.01 for v in values], wall=1002.0))
    assert owner.results["strain_change_01"].current.value == pytest.approx(10 / 6)
    crossing = [0.004] + values[1:]
    owner.accept(mb(3, [-0.004] + [v + 0.02 for v in values[1:]], wall=1003.0))
    owner.accept(mb(4, crossing, wall=1004.0))
    assert owner.results["strain_change_01"].current.value == pytest.approx(8 / 6), "sign change is legal"
    check_consistency(owner)

    # two batches 5 ms apart inside the same second, the second correcting a temperature: the results do not move
    peak = owner.results["thermal_stress_peak"].current
    owner.accept(ua([("temp_wall_007", 481.0)], 1004.500, 2))
    owner.accept(ua([("temp_wall_007", 483.0), ("temp_wall_008", 441.0)], 1004.505, 3))
    check_consistency(owner)
    assert owner.points["temp_wall_007"].obs.value == 483.0 and owner.results["thermal_stress_peak"].current == peak

    # quality changes without any value change: dependents drop out and come back
    owner.accept(bad_round(5, crossing, 4, 1005.0))
    assert owner.results["strain_05"].current.status == r.RESULT_NA
    assert owner.results["stress_05"].current.status == r.RESULT_NA
    assert owner.results["thermal_stress_peak"].current.status == r.RESULT_NA
    check_consistency(owner)
    owner.accept(mb(6, crossing, wall=1006.0))
    assert owner.results["thermal_stress_peak"].current.status == r.RESULT_OK
    check_consistency(owner)

    # a failed round and a skipped round rebuild the strain_change baseline only
    owner.accept(mb(7, [0.0] * 8, outcome="TIMEOUT", wall=1007.0))
    assert all(owner.results[f"strain_{i:02d}"].current.status == r.RESULT_NA for i in range(1, 9))
    assert all(owner.results[f"stress_{i:02d}"].current.status == r.RESULT_NA for i in range(1, 9))
    owner.accept(mb(8, values, wall=1008.0))
    assert owner.results["strain_change_01"].current.reason == "BASELINE_ONLY"
    owner.accept(mb(10, values, wall=1010.0))  # tick 9 skipped
    assert owner.results["strain_change_01"].current.reason == "BASELINE_ONLY"
    owner.accept(mb(11, [v + 0.03 for v in values], wall=1011.0))
    assert owner.results["strain_change_01"].current.value == pytest.approx(30 / 6)
    check_consistency(owner)


def test_output_does_not_depend_on_frame_windows_or_per_point_expiry():
    for cls in (r.CommitGroup, r.Result, r.Observation):
        names = {f.name for f in dataclasses.fields(cls)}
        assert not names & {"window_start", "window_end", "stale_after", "frame_id"}, cls.__name__

    payload = FULL.model_dump(mode="json")
    payload["points"][0]["stale_after"] = 0.001  # the per-point expiry of the old frame chain
    with pytest.raises(ValueError, match="stale_after"):
        FULL.__class__.model_validate(payload)  # is no longer a setting at all, so nothing can depend on it


def test_commit_groups_always_carry_inputs_with_the_results_computed_from_them():
    owner, rec = build()
    owner.accept(mb(1, [0.7] * 8, wall=1001.0))
    group = [g for g in rec.commits if g.kind == r.GROUP_BATCH][-1]
    numbers = [f"{n:02d}" for n in range(1, 9)]
    assert {o.point_id for o in group.observations} == set(BRIDGES)
    assert {x.point_id for x in group.results} == {f"strain_{n}" for n in numbers} | {f"stress_{n}" for n in numbers} | {"thermal_stress_peak"}
    assert {(x.run_id, x.batch_seq) for x in group.results} == {(o.run_id, o.batch_seq) for o in group.observations}
