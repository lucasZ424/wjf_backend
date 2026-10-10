import pytest

from htsg.calculation import calculate
from htsg.config import Derived, Settings, load_settings
from htsg.engine import Engine
from htsg.extremes import is_extreme
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


@pytest.mark.parametrize("scenario", ["baseline", "thermal_step", "load_cycle"])
def test_nominal_simulation_and_thermal_transients_are_not_extreme(scenario):
    config = load_settings("config/research-sim.json")
    simulator = FieldSimulator(config)
    simulator.set_scenario(scenario)
    if scenario == "load_cycle":
        simulator.scenario_started -= simulator.scenario.duration / 2
    previous = None
    for step in range(120):
        simulator.step = step
        simulator.generate(1000 + step)
        values = {}
        for point in config.points:
            value = simulator.values[point.id]["value"]
            assert not is_extreme(value, point), point.id
            values[point.id] = {"value": value, "quality": "GOOD"}
        results = calculate(
            config,
            {
                "run_id": "r",
                "window_start": 1000 + step,
                "window_end": 1001 + step,
                "values": values,
            },
            previous,
        )
        for name, value in results["derived_results"].items():
            if step == 0 and name.startswith("strain_change_"):
                assert value["status"] == "NOT_AVAILABLE"
            else:
                assert value["status"] == "OK" and not value["extreme"], name
        previous = results


def test_extreme_source_value_is_preserved_and_blocks_dependent_calculation():
    config = load_settings("config/research-sim.json")
    records = []
    engine = Engine(config, lambda k, p, urgent: records.append((k, p)), 1000, 0)
    engine.receive("temp_inner_01", 950, 1000.1, 0.1)
    engine.receive("temp_outer_01", 450, 1000.1, 0.1)
    frame = engine.make_frame(1000)
    raw = next(p["sample"] for k, p in records if k == "raw")
    assert raw["value"] == 950 and raw["extreme"] and raw["quality"] == "GOOD"
    assert frame["values"]["temp_inner_01"]["value"] == 950
    assert frame["derived_results"]["stress_01"]["reason"] == "EXTREME_INPUT"


def test_zero_and_signed_strain_are_legitimate_and_derived_extreme_blocks_downstream(settings):
    config = load_settings("config/research-sim.json")
    bridge = next(p for p in config.points if p.id == "bridge_mvv_01")
    assert not is_extreme(0, bridge) and not is_extreme(-6, bridge)
    rules = (
        Derived(id="a", inputs=("x",), operation="sum", unit="test", extreme_high=10),
        Derived(id="b", inputs=("a",), operation="sum", unit="test"),
    )
    frame = {"window_start": 1000, "values": {"x": {"value": 11, "quality": "GOOD"}}}
    result = calculate(settings.model_copy(update={"derived": rules}), frame, None)["derived_results"]
    assert result["a"]["value"] == 11 and result["a"]["extreme"]
    assert result["b"]["value"] is None and result["b"]["reason"] == "EXTREME_INPUT"


def test_different_source_membership_does_not_reject_subscription_or_calculation(settings):
    data = settings.model_dump()
    data["sources"] = (*data["sources"], {"id": "other", "kind": "opcua", "enabled": False})
    data["points"][1]["source"] = "other"
    config = Settings.model_validate(data)
    frame = {
        "window_start": 1000,
        "values": {
            "x": {"value": 5, "quality": "GOOD"},
            "y": {"value": 2, "quality": "GOOD"},
        },
    }
    assert calculate(config, frame, None)["derived_results"]["delta"]["value"] == 3
