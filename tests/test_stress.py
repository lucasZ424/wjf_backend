"""Bridge to stress through the owner, on the configuration the system ships with (验收表 B, 修复轮18).

Why it matters: the customer's walls no longer tell inner from outer, so the stress comes from the bridge alone.  What must hold
at full scale: a round gives strain, stress and peak together or not at all, a wall has no say in any of them, and a stress never
raises a record of its own - it is proportional to the strain, which already does.  Expected values are worked out by hand from
bridge [mV/V] × 1000 / 6 = strain [με] and strain × 0.17 = stress [MPa] (E = 170000 MPa).
"""

import pytest
from archive_support import samples
from fault_support import Accepting, faults_of
from live_support import BRIDGES, FULL, NOMINAL
from owner_support import FixedBucket, Recorder, make_owner, notify, poll, small_config
from pydantic import ValidationError

from htsg import records as r
from htsg.config import Settings, fault_rule
from htsg.owner import Owner

WALL = "temp_wall_001"


def unit(commit=None):
    recorder = Recorder()
    owner = Owner(FULL, 1000.0, 100.0, commit=commit or recorder.commit, buckets=FixedBucket(), run_id="u")
    for source in ("mb_field", "ua_field"):
        owner.begin_generation(source, 1, 1000.0, 100.0)
    return owner, recorder


def round_of(tick, third=0.6, **kw):
    """All bridges at 0.6 mV/V (100 με, 17 MPa); the third as given, None for not answered."""
    values = {b: 0.6 for b in BRIDGES} | {"bridge_mvv_03": third}
    return poll(tick, values, source="mb_field", wall=1000.0 + tick, mono=100.0 + tick, **kw)


def ua(items, seq=1, at=10.0):
    return notify(items, source="ua_field", seq=seq, wall=1000.0 + at, mono=100.0 + at)


def view(owner, name):
    return owner.channel_views()[name]


def test_the_shipped_configuration_has_one_stress_per_bridge_from_its_strain_and_one_peak_over_them():
    """终版 §3.4, Q8: no stress hanging on a temperature, every one on the modulus of 170000 MPa until the steel is known."""
    assert [m.young_mpa for m in FULL.models] == [170000]
    stress = {d.id: d for d in FULL.derived if d.operation == "elastic_stress"}
    assert sorted(stress) == [f"stress_{n:02d}" for n in range(1, 9)]
    for n in range(1, 9):
        rule = stress[f"stress_{n:02d}"]
        assert rule.inputs == (f"strain_{n:02d}",) and rule.unit == "MPa" and rule.model_id == FULL.models[0].id
    peak = next(d for d in FULL.derived if d.id == "thermal_stress_peak")
    assert peak.operation == "absolute_peak" and peak.inputs == tuple(sorted(stress))


def test_a_bridge_reading_becomes_a_strain_and_a_stress_by_hand_calculation():
    """B4: 0.708 mV/V -> 118 με -> 20.06 MPa.  A stress computed straight from the mV/V would read 0.12."""
    owner, _ = unit()
    owner.accept(round_of(1, third=0.708))
    assert view(owner, "bridge_mvv_03")["value"] == 0.708, "the raw reading is kept apart from what it is converted to"
    assert view(owner, "strain_03")["value"] == pytest.approx(118.0)
    assert view(owner, "stress_03")["value"] == pytest.approx(20.06)
    assert view(owner, "stress_01")["value"] == pytest.approx(17.0)
    assert view(owner, "thermal_stress_peak")["value"] == pytest.approx(20.06)


@pytest.mark.parametrize(
    "lost",
    [
        pytest.param(lambda tick: round_of(tick, third=None), id="not-answered"),
        pytest.param(lambda tick: r.InputBatch(
            "mb_field", 1, r.KIND_MODBUS_POLL, 1000.0 + tick, 100.0 + tick,
            tuple(r.Item(b, 0.6, r.BAD if b == "bridge_mvv_03" else r.GOOD, 0x80350000 if b == "bridge_mvv_03" else None, response_id="resp0") for b in BRIDGES),
            tick=tick, poll_id=f"poll{tick}", outcome="OK",
        ), id="bad-quality"),
    ],
)
def test_a_bridge_lost_for_a_round_leaves_no_strain_stress_or_peak_and_the_next_round_brings_them_back(lost):
    """B5, 终版 §6, §8: neither the old value nor a 0 stands in, and the other bridges are not touched."""
    owner, recorder = unit()
    owner.accept(round_of(1))
    owner.accept(lost(2))
    for name in ("strain_03", "stress_03", "thermal_stress_peak"):
        now = view(owner, name)
        assert (now["valid"], now["value"], now["reason"]) == (False, None, "INPUT_UNAVAILABLE"), name
    assert view(owner, "stress_01")["value"] == pytest.approx(17.0), "the bridge that answered still computes"
    assert recorder.results("stress_03")[-1].value is None, "history says so too: a result with no value"
    owner.accept(round_of(3, third=0.708))
    assert view(owner, "stress_03")["value"] == pytest.approx(20.06) and view(owner, "stress_03")["valid"]
    assert view(owner, "thermal_stress_peak")["value"] == pytest.approx(20.06)


def test_a_failed_round_takes_every_stress_away_and_the_peak_with_them():
    """B5: a whole round without an answer."""
    owner, _ = unit()
    owner.accept(round_of(1))
    owner.accept(poll(2, {b: 0.0 for b in BRIDGES}, outcome="TIMEOUT", source="mb_field", wall=1002.0, mono=102.0))
    for n in range(1, 9):
        assert view(owner, f"stress_{n:02d}")["value"] is None
    assert view(owner, "thermal_stress_peak")["value"] is None
    owner.accept(round_of(3))
    assert view(owner, "stress_01")["value"] == pytest.approx(17.0)


@pytest.mark.parametrize(
    ("bridge", "strain", "stress"),
    [
        pytest.param(17.646, 2941.0, 499.97, id="B7-about-500-MPa"),
        pytest.param(30.0, 5000.0, 850.0, id="B7-850-MPa-the-old-limit-of-500-would-call-this-extreme"),
        pytest.param(-30.0, -5000.0, -850.0, id="B7-minus-850-MPa"),
    ],
)
def test_a_strain_still_within_its_own_limit_never_makes_an_extreme_stress(bridge, strain, stress):
    """B7, Q4: ±850 MPa is ±5000 με at this modulus, so while a strain is valid its stress cannot be judged extreme first."""
    owner, _ = unit()
    owner.accept(round_of(1, third=bridge))
    assert view(owner, "strain_03")["value"] == pytest.approx(strain) and not view(owner, "strain_03")["extreme"]
    now = view(owner, "stress_03")
    assert now["value"] == pytest.approx(stress) and not now["extreme"] and now["valid"]
    peak = view(owner, "thermal_stress_peak")
    assert peak["value"] == pytest.approx(stress) and not peak["extreme"] and peak["valid"]


def boundary_config():
    """A bridge whose strain has no limit of its own, so that the limit of the stress itself is what decides."""
    return small_config(
        extra_points=[{"id": "b9", "source": "mb", "unit": "mV/V", "address": 4, "data_type": "float32"}],
        extra_derived=[
            {"id": "s9", "inputs": ["b9"], "operation": "bridge_strain", "unit": "με"},
            {"id": "st9", "inputs": ["s9"], "operation": "elastic_stress", "unit": "MPa", "model_id": "elastic", "extreme_low": -850, "extreme_high": 850},
            {"id": "pk9", "inputs": ["st9"], "operation": "absolute_peak", "unit": "MPa", "extreme_low": -850, "extreme_high": 850},
        ],
    )  # fmt: skip


def test_a_stress_beyond_850_is_extreme_at_850_it_is_not_and_what_rests_on_it_gets_no_number():
    """B8, 终版 §8: strictly beyond the limit; the value stays, marked, and the calculation does not go on from it."""
    owner, _ = make_owner(boundary_config(), generations=("mb",))
    owner.accept(poll(1, {"b9": 30.0}))  # 5000 με, 850 MPa
    assert view(owner, "st9")["value"] == pytest.approx(850.0) and not view(owner, "st9")["extreme"] and view(owner, "st9")["valid"]
    assert view(owner, "pk9")["valid"]
    owner.accept(poll(2, {"b9": 850.1 / 0.17 * 6 / 1000}))
    assert view(owner, "st9")["value"] == pytest.approx(850.1) and view(owner, "st9")["extreme"] and not view(owner, "st9")["valid"]
    assert view(owner, "pk9")["value"] is None and view(owner, "pk9")["reason"] == "EXTREME_INPUT"


def test_no_stress_has_a_limit_and_a_strain_is_the_one_that_raises_the_record():
    """B10, Q7: a stress is proportional to the strain; a limit on it would only say the same thing twice."""
    assert not [d.id for d in FULL.derived if d.unit == "MPa" and fault_rule(d)]
    sink = Accepting()
    owner = Owner(FULL, 1000.0, 100.0, commit=sink, buckets=FixedBucket(), run_id="u")
    sink.owner = owner
    for source in ("mb_field", "ua_field"):
        owner.begin_generation(source, 1, 1000.0, 100.0)
    owner.accept(ua(list(NOMINAL.items()), at=0.5))
    for tick in range(1, 11):  # 700 με (119 MPa) on the third, then the highest legal stress on the fifth, held for ten seconds
        values = {b: 0.6 for b in BRIDGES} | {"bridge_mvv_03": 4.2, "bridge_mvv_05": 30.0}
        owner.accept(poll(tick, values, source="mb_field", wall=1000.0 + tick, mono=100.0 + tick))
        owner.accept(ua([], seq=tick + 1, at=float(tick)))
        owner.tick(1000.0 + tick, 100.0 + tick)
    assert view(owner, "stress_05")["value"] == pytest.approx(850.0)
    assert sorted(f.point_id for f in faults_of(sink)) == ["strain_03", "strain_05"], "the strains, and no stress or peak"


@pytest.mark.parametrize(
    "items",
    [
        pytest.param([(WALL, 700.0)], id="a-wall-changes"),
        pytest.param([(WALL, 480.0, r.BAD, 0x80350000)], id="a-wall-goes-bad"),
        pytest.param([(f"temp_wall_{n:03d}", 700.0 + n) for n in range(1, 216)], id="every-wall-changes"),
    ],
)
def test_a_wall_has_no_say_in_any_stress_or_in_the_peak(items):
    """B11 (用户 11-05): the stress used to hang on the wall temperatures; now nothing connects them.  C6: no derived result either."""
    owner, recorder = unit()
    owner.accept(round_of(1, third=0.708))
    before = {n: view(owner, n) for n in ("stress_03", "strain_03", "thermal_stress_peak")}
    results = len(recorder.results())
    owner.accept(ua(items))
    assert {n: view(owner, n) for n in before} == before
    assert len(recorder.results()) == results, "a wall's batch stores no result at all"


def test_opcua_going_down_takes_the_walls_and_leaves_the_stress_alone():
    """The backend side of F5 (用户 11-05): two sources, and neither stands in for the other."""
    owner, _ = unit()
    owner.accept(ua(list(NOMINAL.items()), at=0.5))
    owner.accept(round_of(1))
    owner.source_down("ua_field", 1, "TEST", 1002.0, 102.0)
    assert not view(owner, WALL)["valid"] and not view(owner, "load_mw")["valid"]
    assert view(owner, "stress_01")["valid"] and view(owner, "stress_01")["value"] == pytest.approx(17.0)
    assert view(owner, "thermal_stress_peak")["valid"]


def test_modbus_going_down_takes_the_stress_and_leaves_the_walls_alone():
    """The backend side of F6 (用户 11-05)."""
    owner, _ = unit()
    owner.accept(ua(list(NOMINAL.items()), at=0.5))
    owner.accept(round_of(1))
    owner.source_down("mb_field", 1, "TEST", 1002.0, 102.0)
    for name in ("stress_01", "strain_01", "thermal_stress_peak"):
        assert not view(owner, name)["valid"] and view(owner, name)["value"] is None, name
    assert view(owner, WALL)["valid"] and view(owner, "load_mw")["valid"]


def test_a_commit_group_of_a_round_holds_the_readings_and_everything_computed_from_them():
    """终版 §6: inputs and their results are one group.  The change of the first round waits for the second."""
    owner, recorder = unit()
    owner.accept(round_of(1))
    (group,) = [g for g in recorder.commits if g.kind == r.GROUP_BATCH]
    assert {o.point_id for o in group.observations} == set(BRIDGES)
    numbers = [f"{n:02d}" for n in range(1, 9)]
    assert {x.point_id for x in group.results} == {f"strain_{n}" for n in numbers} | {f"stress_{n}" for n in numbers} | {"thermal_stress_peak"}
    owner.accept(round_of(2, third=0.708))
    group = [g for g in recorder.commits if g.kind == r.GROUP_BATCH][-1]
    assert {x.point_id for x in group.results} == {"strain_03", "stress_03", "strain_change_01", "strain_change_02", "strain_change_03",
                                                   "strain_change_04", "strain_change_05", "strain_change_06", "strain_change_07",
                                                   "strain_change_08", "thermal_stress_peak"}  # fmt: skip


def test_only_the_batch_end_result_of_an_algebraic_result_reaches_the_real_archive(archive_db_factory):
    """Stands in for the replay of delta_t_05 (Capstone F) that had three versions at one instant: 终版 §8.2 keeps the rule, the
    default point table has no OPC UA input with a result any more (C8).  All three versions are stored in batch order, one
    result is calculated, and only from the last."""
    store = archive_db_factory()
    owner, recorder = make_owner(generations=("ua",))
    store.initialize(owner.config)
    store.bind_run("run", owner.config.config_id, 1000.0)
    owner.accept(notify([("to", 440.0), ("ti", 481.0), ("ti", 482.0), ("ti", 483.0)]))
    for group in recorder.commits:
        assert store.write_group(group).durable
    with store.session() as conn:
        versions = samples(conn, "c.point_id='ti'")
        results = samples(conn, "c.point_id='dt'")
    assert [x["value"] for x in versions] == [481.0, 482.0, 483.0], "in batch order"
    assert len({x["t"] for x in versions}) == 1, "they share one time"
    assert [x["value"] for x in results] == [43.0], "483 - 440, and nothing from the versions before it"


def test_a_bad_input_of_an_algebraic_result_leaves_it_not_available_in_the_real_archive_and_it_comes_back(archive_db_factory):
    """Stands in for delta_t_06 in the replay (Capstone F), and the same for a bridge is in that scenario now: the number stays
    with the reading, the result says it has none, and a Good reading of the same number brings the result back."""
    store = archive_db_factory()
    owner, recorder = make_owner(generations=("ua",))
    store.initialize(owner.config)
    store.bind_run("run", owner.config.config_id, 1000.0)
    owner.accept(notify([("ti", 480.0), ("to", 440.0)], seq=1, wall=1001.0, mono=101.0))
    owner.accept(notify([("ti", 480.0, r.BAD, 0x80350000)], seq=2, wall=1002.0, mono=102.0))
    owner.accept(notify([("ti", 480.0)], seq=3, wall=1003.0, mono=103.0))
    for group in recorder.commits:
        assert store.write_group(group).durable
    with store.session() as conn:
        raw = samples(conn, "c.point_id='ti'")
        results = samples(conn, "c.point_id='dt'")
    assert [(x["t"], x["value"], x["status_code"]) for x in raw] == [(1001.0, 480.0, None), (1002.0, 480.0, 0x80350000), (1003.0, 480.0, None)]
    assert [(x["t"], x["value"]) for x in results] == [(1001.0, 40.0), (1002.0, None), (1003.0, 40.0)]


@pytest.mark.parametrize(
    ("rule", "why"),
    [
        pytest.param({"inputs": ["b1"]}, "the input is in mV/V: the conversion was skipped", id="B6-input-mV-per-V"),
        pytest.param({"inputs": ["ti"]}, "a temperature: the model that is gone", id="B6-input-temperature"),
        pytest.param({"inputs": ["s1", "s2"]}, "two inputs: one bridge, one stress", id="B6-two-inputs"),
        pytest.param({"unit": "kPa"}, "the result is in MPa", id="B6-unit"),
        pytest.param({"scale": 2}, "a scale on top of the modulus", id="B6-scale"),
        pytest.param({"offset": 1}, "an offset on top of the modulus", id="B6-offset"),
        pytest.param({"model_id": "missing"}, "no such model", id="B6-unknown-model"),
    ],
)
def test_a_stress_rule_that_is_not_one_strain_times_a_model_is_refused(rule, why):
    """B6, 热应力估算模型 配置与单位: a mistake in the file stops the start instead of giving a wrong number for a year."""
    good = {"id": "st", "inputs": ["s1"], "operation": "elastic_stress", "unit": "MPa", "model_id": "elastic"}
    assert Settings.model_validate(small_config(extra_derived=[good]).model_dump()), "the rule is fine as it stands"
    with pytest.raises(ValidationError):
        small_config(extra_derived=[{**good, **rule}])
    assert why


def test_a_model_on_an_operation_other_than_the_stress_is_refused():
    with pytest.raises(ValidationError):
        small_config(extra_derived=[{"id": "x", "inputs": ["ti", "to"], "operation": "difference", "unit": "degC", "model_id": "elastic"}])


def test_the_old_operator_is_refused():
    """A5 (用户 11-05): a configuration still using it must not start."""
    with pytest.raises(ValidationError):
        small_config(extra_derived=[{"id": "x", "inputs": ["ti", "to"], "operation": "thermal_stress", "unit": "MPa", "model_id": "elastic"}])
