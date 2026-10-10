from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from htsg.calculation import calculate
from htsg.thermal import ThermalModel, estimate_thermal


def model(**updates):
    return ThermalModel.model_validate(
        {"id": "research", "young_mpa": 200000, "expansion_per_k": 1e-5, "restraint": 0.5, **updates}
    )


def test_independent_hand_calculation_and_parameter_envelope():
    result = estimate_thermal(model(), 610, 600)
    # 200 GPa * 10 microstrain/K * 10 K * one-half restraint = 10 MPa.
    assert result["value"] == pytest.approx(10)
    assert result["estimate"]["parameter_range_mpa"] == pytest.approx([4.86, 16.94])
    assert result["estimate"]["qualification"] == "ESTIMATED"
    assert result["estimate"]["model_id"] == "research"


def test_input_order_controls_sign_and_envelope_remains_ordered():
    result = estimate_thermal(model(), 600, 610)
    assert result["value"] == pytest.approx(-10)
    assert result["estimate"]["parameter_range_mpa"] == pytest.approx([-16.94, -4.86])


def test_uniform_heating_has_no_temperature_difference_proxy():
    for temperature in (20, 600):
        result = estimate_thermal(model(), temperature, temperature)
        assert result["value"] == 0
        assert result["estimate"]["parameter_range_mpa"] == [0, 0]


def test_unrestrained_and_fully_restrained_limits():
    free = estimate_thermal(model(restraint=0, restraint_uncertainty=0), 610, 600)
    assert free["value"] == 0
    assert free["estimate"]["parameter_range_mpa"] == [0, 0]
    fixed = estimate_thermal(model(restraint=1), 610, 600)
    assert fixed["value"] == pytest.approx(20)
    # Upper restraint stays at one, even when its configured sensitivity extends beyond it.
    assert fixed["estimate"]["parameter_range_mpa"][1] == pytest.approx(24.2)


@pytest.mark.parametrize(
    "updates",
    [
        {"young_mpa": 0},
        {"expansion_per_k": -1},
        {"restraint": 1.1},
        {"young_relative_uncertainty": 1},
        {"min_temperature_c": 800},
        {"expansion_per_k": float("nan")},
    ],
)
def test_invalid_research_assumptions_rejected_at_configuration(updates):
    with pytest.raises(ValidationError):
        model(**updates)


@pytest.mark.parametrize(
    "temperature,reason", [(801, "TEMPERATURE_OUTSIDE_MODEL_RANGE"), (float("inf"), "NONFINITE")]
)
def test_no_silent_extrapolation_or_nonfinite_result(temperature, reason):
    with pytest.raises(ValueError, match=reason):
        estimate_thermal(model(), temperature, 600)


def calculate_stress(**input_overrides):
    config = SimpleNamespace(
        models=(model(),),
        derived=(
            SimpleNamespace(
                id="stress",
                inputs=("inner", "outer"),
                operation="thermal_stress",
                model_id="research",
                max_age=2,
                extreme_low=None,
                extreme_high=None,
                scale=1,
                offset=0,
            ),
        ),
    )
    inner = {"value": 610, "quality": "GOOD", **input_overrides}
    frame = {
        "run_id": "r",
        "window_start": 100,
        "window_end": 101,
        "values": {"inner": inner, "outer": {"value": 600, "quality": "GOOD"}},
    }
    return calculate(config, frame, None)["derived_results"]["stress"]


def test_frame_preserves_estimate_metadata_and_held_input_age():
    result = calculate_stress(basis="HELD_VALUE", basis_time=100)
    assert result["status"] == "OK" and result["value"] == pytest.approx(10)
    assert result["max_input_age"] == 1
    assert result["estimate"]["parameters"]["young_mpa"] == 200000


@pytest.mark.parametrize(
    "input_overrides",
    [{"quality": "BAD_TIME"}, {"value": None}, {"basis": "HELD_VALUE", "basis_time": 90}],
)
def test_research_label_does_not_bypass_input_quality_or_freshness(input_overrides):
    result = calculate_stress(**input_overrides)
    assert result["status"] == "NOT_AVAILABLE" and result["value"] is None
    assert result["reason"] == "INPUT_UNAVAILABLE"


def test_outside_research_temperature_range_is_visible_on_frame():
    result = calculate_stress(value=850)
    assert result["status"] == "NOT_AVAILABLE" and result["value"] is None
    assert result["reason"] == "TEMPERATURE_OUTSIDE_MODEL_RANGE"
