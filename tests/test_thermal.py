from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from htsg.derive import Input, evaluate
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


def outcome(inner=610.0, valid=True):
    """The stress rule as the owner applies it at the end of a batch: from the inputs' validity, nothing else."""
    rule = SimpleNamespace(
        id="stress", inputs=("inner", "outer"), operation="thermal_stress", model_id="research",
        extreme_low=None, extreme_high=None, scale=1, offset=0,
    )  # fmt: skip
    return evaluate(rule, [Input(inner, valid), Input(600.0, True)], {"research": model()})


def test_result_preserves_estimate_metadata():
    result = outcome()
    assert result.status == "OK" and result.value == pytest.approx(10)
    assert result.estimate["parameters"]["young_mpa"] == 200000


@pytest.mark.parametrize("inner,valid", [(610.0, False), (None, True)])
def test_research_label_does_not_bypass_input_quality(inner, valid):
    result = outcome(inner, valid)
    assert result.status == "NOT_AVAILABLE" and result.value is None
    assert result.reason == "INPUT_UNAVAILABLE"


def test_outside_research_temperature_range_is_visible_on_the_result():
    result = outcome(850.0)
    assert result.status == "NOT_AVAILABLE" and result.value is None
    assert result.reason == "TEMPERATURE_OUTSIDE_MODEL_RANGE"
