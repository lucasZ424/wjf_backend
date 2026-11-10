"""The point thermal stress estimate as a formula (dev-docs/热应力估算模型.md, 修复轮18): stress [MPa] = E [MPa] × strain [με] × 10⁻⁶.

Why it matters: the number on the fourth page is read as a stress.  A wrong power of ten, a modulus fixed in the code or a lost
sign gives a figure that looks just as plausible; the expected values here are worked out by hand.  The chain through the owner
is in test_stress.py.  The old temperature-difference model and its fields are gone: a configuration that still names them must
be refused, or it would run on silently with a meaning it no longer has.
"""

from types import SimpleNamespace

import pytest
from owner_support import small_config
from pydantic import ValidationError

from htsg.derive import Input, evaluate


def model(**updates):
    return small_config(models=[{"id": "m", **updates}]).models[0]


def outcome(strain, valid=True, young=170000, models=None):
    """The stress rule as the owner applies it at the end of a batch."""
    rule = SimpleNamespace(
        id="stress", inputs=("strain",), operation="elastic_stress", model_id="m",
        extreme_low=-850, extreme_high=850, scale=1, offset=0,
    )  # fmt: skip
    return evaluate(rule, [Input(strain, valid)], {"m": model(young_mpa=young)} if models is None else models)


@pytest.mark.parametrize(
    ("young", "strain", "stress"),
    [
        pytest.param(170000, 690.0, 117.3, id="B1-a-factor-of-1e-3-or-1e-9-is-off-by-orders-of-magnitude"),
        pytest.param(200000, 100.0, 20.0, id="B2-E-comes-from-the-model-not-the-code"),
        pytest.param(200000, -100.0, -20.0, id="B3-the-sign-of-the-strain-is-kept"),
    ],
)
def test_hand_calculations(young, strain, stress):
    result = outcome(strain, young=young)
    assert result.status == "OK" and result.reason is None
    assert result.value == pytest.approx(stress)


def test_no_strain_is_no_stress_and_a_true_zero_is_a_value():
    """A zero is a reading (终版 §8), not a missing value and not a reason to refuse."""
    result = outcome(0.0)
    assert result.status == "OK" and result.value == 0


@pytest.mark.parametrize(("strain", "valid"), [(690.0, False), (None, True)])
def test_a_missing_or_invalid_strain_gives_no_number(strain, valid):
    """B5 (热应力估算模型 结果表达): neither the old value nor 0 stands in for a strain that is not there."""
    result = outcome(strain, valid)
    assert result.status == "NOT_AVAILABLE" and result.value is None
    assert result.reason == "INPUT_UNAVAILABLE"


def test_a_result_that_cannot_be_represented_is_not_a_number_on_the_page():
    result = outcome(1e308)
    assert result.status == "NOT_AVAILABLE" and result.value is None and result.reason == "NONFINITE"


def test_a_rule_whose_model_is_not_there_gives_no_number_instead_of_crashing_the_batch():
    """The hard rule: the check at configuration time should stop this, but a calculation must not end the process either way."""
    result = outcome(100.0, models={})
    assert result.status == "NOT_AVAILABLE" and result.value is None and result.reason == "MODEL_NOT_CONFIGURED"


@pytest.mark.parametrize(
    ("strain", "extreme"),
    [
        pytest.param(5000.0, False, id="B8-850-MPa-on-the-limit-is-not-extreme"),
        pytest.param(850.1 / 0.17, True, id="B8-850.1-MPa-beyond-the-limit-is-extreme"),
        pytest.param(-5000.0, False, id="B8-minus-850-MPa-on-the-limit-is-not-extreme"),
        pytest.param(-850.1 / 0.17, True, id="B8-minus-850.1-MPa-beyond-the-limit-is-extreme"),
    ],
)
def test_the_extreme_limit_of_a_stress_is_strict_and_the_value_is_kept(strain, extreme):
    """B8, 默认极值阈值 §2: only strictly beyond, and a result beyond its own limit keeps its value with the mark."""
    result = outcome(strain)
    assert result.status == "OK" and result.extreme is extreme
    assert result.value == pytest.approx(strain * 0.17)


def test_the_model_takes_only_the_modulus_and_what_says_where_it_comes_from():
    """Q8: 170000 MPa until the customer names the steel; the version, the basis and the links record that assumption."""
    default = model()
    assert default.young_mpa == 170000
    fuller = model(young_mpa=200000, version="v2", basis="steel sheet", reference_urls=["https://example.invalid/steel"])
    assert (fuller.young_mpa, fuller.version, fuller.basis, fuller.reference_urls) == (200000, "v2", "steel sheet", ("https://example.invalid/steel",))


@pytest.mark.parametrize("young", [0, -1, float("nan"), float("inf")])
def test_an_impossible_modulus_is_refused_at_configuration(young):
    with pytest.raises(ValidationError):
        model(young_mpa=young)


@pytest.mark.parametrize(
    "field",
    [
        pytest.param({"restraint": 0.5}, id="A5-restraint"),
        pytest.param({"expansion_per_k": 1.35e-5}, id="A5-expansion"),
        pytest.param({"restraint_uncertainty": 0.1}, id="A5-restraint-range"),
        pytest.param({"young_relative_uncertainty": 0.1}, id="A5-modulus-range"),
        pytest.param({"min_temperature_c": 0}, id="A5-temperature-range"),
    ],
)
def test_a_configuration_with_the_old_model_fields_is_refused(field):
    """A5: such a file was written for a stress from a temperature difference; it must not start with another meaning."""
    with pytest.raises(ValidationError):
        model(**field)
