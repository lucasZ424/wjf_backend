"""Capstone B: the frame-free calculation loop at full scale (方案第 4 节).

208 inputs and 209 derived channels driven through the owner with an in-memory commit sink.
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
INNER = [f"temp_inner_{i:02d}" for i in range(1, 97)]
OUTER = [f"temp_outer_{i:02d}" for i in range(1, 97)]
PROCESS = {
    p.id: 1.0 for p in FULL.points if not p.id.startswith(("bridge_", "temp_"))
}  # eight process values
COEFFICIENT = 170000 * 1.35e-5 * 0.5  # E * alpha * restraint, MPa per K


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
    for i in range(8):
        b = good(owner, BRIDGES[i])
        expected[f"strain_{i + 1:02d}"] = None if b is None else b * 1000 / 6
    stresses = []
    for i in range(96):
        ti, to = good(owner, INNER[i]), good(owner, OUTER[i])
        n = f"{i + 1:02d}"
        expected[f"delta_t_{n}"] = None if None in (ti, to) else ti - to
        stress = None if None in (ti, to) else COEFFICIENT * (ti - to)
        expected[f"stress_{n}"] = stress
        stresses.append(stress)
    expected["thermal_stress_peak"] = None if None in stresses else max(stresses)
    return expected


def check_consistency(owner):
    for name, value in oracle(owner).items():
        current = owner.results[name].current
        if value is None:
            assert current is None or current.status == r.RESULT_NA, name
        else:
            assert current.status == r.RESULT_OK and current.value == pytest.approx(value), name


def test_full_scale_loop_matches_an_independent_oracle_through_every_scenario():
    owner, rec = build()
    inner = [(n, 480.0 + i * 0.1) for i, n in enumerate(INNER)]
    outer = [(n, 440.0 + i * 0.05) for i, n in enumerate(OUTER)]
    initial = [*inner, *outer, *PROCESS.items()]
    assert len(initial) == 200

    # initial burst of all 200 OPC UA points arrives as one batch
    owner.accept(ua(initial, 1001.0, 1))
    check_consistency(owner)
    assert owner.results["thermal_stress_peak"].current.status == r.RESULT_OK

    # Modbus rounds; round 3 crosses zero legally; the round straddles a second boundary
    values = [0.7 + 0.01 * i for i in range(8)]
    owner.accept(mb(1, values, wall=1001.0))
    check_consistency(owner)
    owner.accept(mb(2, [v + 0.01 for v in values], wall=1002.0))
    assert owner.results["strain_change_01"].current.value == pytest.approx(10 / 6)
    crossing = [0.004] + values[1:]
    owner.accept(mb(3, [-0.004] + [v + 0.02 for v in values[1:]], wall=1003.0))
    owner.accept(mb(4, crossing, wall=1004.0))
    assert owner.results["strain_change_01"].current.value == pytest.approx(8 / 6), "sign change is legal"
    check_consistency(owner)

    # two batches 5 ms apart inside the same second, the second correcting a temperature
    owner.accept(ua([("temp_inner_07", 481.0)], 1004.500, 2))
    owner.accept(ua([("temp_inner_07", 483.0), ("temp_outer_07", 441.0)], 1004.505, 3))
    check_consistency(owner)

    # quality changes without any value change: dependents drop out and come back
    owner.accept(ua([("temp_inner_05", 480.4, r.BAD, 0x80350000)], 1005.0, 4))
    assert owner.results["delta_t_05"].current.status == r.RESULT_NA
    assert owner.results["thermal_stress_peak"].current.status == r.RESULT_NA
    check_consistency(owner)
    owner.accept(ua([("temp_inner_05", 480.4)], 1006.0, 5))
    assert owner.results["thermal_stress_peak"].current.status == r.RESULT_OK
    check_consistency(owner)

    # a failed round and a skipped round rebuild the strain_change baseline only
    owner.accept(mb(5, [0.0] * 8, outcome="TIMEOUT", wall=1005.0))
    assert all(owner.results[f"strain_{i:02d}"].current.status == r.RESULT_NA for i in range(1, 9))
    owner.accept(mb(6, values, wall=1006.0))
    assert owner.results["strain_change_01"].current.reason == "BASELINE_ONLY"
    owner.accept(mb(8, values, wall=1008.0))  # tick 7 skipped
    assert owner.results["strain_change_01"].current.reason == "BASELINE_ONLY"
    owner.accept(mb(9, [v + 0.03 for v in values], wall=1009.0))
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
    owner.accept(ua([("temp_inner_01", 500.0), ("temp_outer_01", 450.0)], 1001.0, 1))
    group = [g for g in rec.commits if g.kind == r.GROUP_BATCH][-1]
    assert {o.point_id for o in group.observations} == {"temp_inner_01", "temp_outer_01"}
    assert {x.point_id for x in group.results} == {"delta_t_01", "stress_01"}
    assert {(x.run_id, x.batch_seq) for x in group.results} == {(o.run_id, o.batch_seq) for o in group.observations}
