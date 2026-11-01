import pytest
from owner_support import make_owner, notify, poll, small_config

from htsg.config import load_settings
from htsg.extremes import is_extreme
from htsg.owner import Owner
from htsg.simulation import FieldSimulator


def test_defaults_cover_all_current_source_and_derived_channels():
    config = load_settings("config/research-sim.json")
    assert len(config.points) == 208 and len(config.derived) == 209
    for point in (*config.points, *config.derived):
        assert point.extreme_low is not None and point.extreme_high is not None
        assert not is_extreme(point.extreme_low, point)
        assert not is_extreme(point.extreme_high, point)
        assert is_extreme(point.extreme_low - 1, point)
        assert is_extreme(point.extreme_high + 1, point)


@pytest.mark.parametrize("scenario", ["baseline", "load_ramp", "hotspot_01", "overload"])
def test_nominal_simulation_and_thermal_transients_are_not_extreme(scenario):
    """The defaults leave room: nothing the simulated plant does in its process scenarios - the ordinary limits it
    crosses included - is flagged as extreme, and every result is calculated (only the first strain change waits for
    a second round)."""
    config = load_settings("config/research-sim.json")
    simulator = FieldSimulator(config)
    simulator.set_scenario(scenario)
    if scenario != "baseline":  # straight to the hold: the walls and flows then run through their lags meanwhile
        simulator.scenario_started -= simulator.scenario.duration / 2
    owner = Owner(config, 1000.0, 100.0, run_id="r")
    for source in ("mb_field", "ua_field"):
        owner.begin_generation(source, 1, 1000.0, 100.0)
    by_source = {s.id: [p for p in config.points if p.source == s.id] for s in config.sources}
    for step in range(120):
        simulator.step = step
        simulator.generate(1000 + step)
        values = {p.id: simulator.values[p.id]["value"] for p in config.points}
        assert not [p.id for p in config.points if is_extreme(values[p.id], p)]
        wall, mono = 1000.0 + step, 100.0 + step
        owner.accept(notify([(p.id, values[p.id]) for p in by_source["ua_field"]], source="ua_field", wall=wall, mono=mono, seq=step + 1))
        owner.accept(poll(step + 1, {p.id: values[p.id] for p in by_source["mb_field"]}, source="mb_field", wall=wall, mono=mono))
        for name, view in owner.channel_views().items():
            if view["kind"] != "derived":
                continue
            if step == 0 and name.startswith("strain_change_"):
                assert view["reason"] == "BASELINE_ONLY"
            else:
                assert view["valid"] and not view["extreme"], (scenario, step, name, view["reason"])


def test_zero_and_signed_strain_are_legitimate_inputs():
    config = load_settings("config/research-sim.json")
    bridge = next(p for p in config.points if p.id == "bridge_mvv_01")
    assert not is_extreme(0, bridge) and not is_extreme(-6, bridge)


def test_a_result_is_calculated_from_inputs_of_different_sources():
    config = small_config(
        extra_points=[{"id": "pa", "source": "mb", "unit": "degC", "address": 4, "data_type": "float32"}],
        extra_derived=[{"id": "mix", "inputs": ["ti", "pa"], "operation": "difference", "unit": "degC"}],
    )
    owner, _ = make_owner(config)
    owner.accept(notify([("ti", 500.0)]))
    owner.accept(poll(1, {"pa": 200.0}))
    assert owner.channel_views()["mix"]["value"] == 300.0
