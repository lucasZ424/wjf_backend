"""Transparent temperature-difference research proxy, not a structural stress solution."""

import math

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ThermalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    id: str = Field(min_length=1)
    young_mpa: float = Field(170000, gt=0, le=1000000)
    expansion_per_k: float = Field(1.35e-5, gt=0, le=0.001)
    restraint: float = Field(0.5, ge=0, le=1)
    young_relative_uncertainty: float = Field(0.1, ge=0, lt=1)
    expansion_relative_uncertainty: float = Field(0.1, ge=0, lt=1)
    restraint_uncertainty: float = Field(0.2, ge=0, le=1)
    min_temperature_c: float = Field(-50, ge=-273.15)
    max_temperature_c: float = 800
    version: str = Field("1", min_length=1)
    basis: str = Field("synthetic research assumption", min_length=1)
    reference_urls: tuple[str, ...] = ()

    @model_validator(mode="after")
    def temperature_range(self):
        if self.min_temperature_c >= self.max_temperature_c:
            raise ValueError("model temperature range must increase")
        return self


def estimate_thermal(model: ThermalModel, inner_c: float, outer_c: float) -> dict:
    """Return signed proxy and interval from independent parameter extrema, in MPa.

    The sign follows input order, not tensile/compressive stress conventions.
    The interval excludes unknown geometry, temperature errors and model-form error.
    """
    if not all(math.isfinite(t) for t in (inner_c, outer_c)):
        raise ValueError("NONFINITE")
    if not all(model.min_temperature_c <= t <= model.max_temperature_c for t in (inner_c, outer_c)):
        raise ValueError("TEMPERATURE_OUTSIDE_MODEL_RANGE")
    delta = inner_c - outer_c
    coefficient = model.young_mpa * model.expansion_per_k
    minimum = (
        coefficient
        * (1 - model.young_relative_uncertainty)
        * (1 - model.expansion_relative_uncertainty)
        * max(0, model.restraint - model.restraint_uncertainty)
    )
    maximum = (
        coefficient
        * (1 + model.young_relative_uncertainty)
        * (1 + model.expansion_relative_uncertainty)
        * min(1, model.restraint + model.restraint_uncertainty)
    )
    value = coefficient * model.restraint * delta
    parameter_range = sorted((minimum * delta, maximum * delta))
    if not all(math.isfinite(x) for x in (value, *parameter_range)):
        raise ValueError("NONFINITE")
    return {
        "value": value,
        "estimate": {
            "kind": "RESEARCH_PROXY",
            "qualification": "ESTIMATED",
            "model_id": model.id,
            "model_version": model.version,
            "temperature_difference_k": delta,
            "parameter_range_mpa": parameter_range,
            "parameters": model.model_dump(exclude={"id", "version", "basis", "reference_urls"}),
            "basis": model.basis,
            "reference_urls": list(model.reference_urls),
            "sign_convention": "positive when first temperature exceeds second",
            "range_meaning": "parameter sensitivity only; excludes input and model-form errors",
        },
    }
