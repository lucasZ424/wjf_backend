"""Pure Frame-only mathematical operators, executed by the runtime calculation worker."""

import math
import statistics

from htsg.extremes import is_extreme
from htsg.thermal import estimate_thermal


def input_age(value, window_end):
    if value.get("basis") == "HELD_VALUE":
        return window_end - value["basis_time"]
    return value.get("max_input_age", 0)


def calculate(config, frame, previous):
    values = frame["values"]
    start = frame["window_start"]
    derived_results = {}
    for rule in config.derived:
        inputs = [values.get(p, derived_results.get(p)) for p in rule.inputs]
        result = {"value": None, "status": "NOT_AVAILABLE", "reason": "INPUT_UNAVAILABLE"}
        if rule.operation == "unconfigured":
            result["reason"] = "MODEL_NOT_CONFIGURED"
        elif any(x and x.get("extreme", False) for x in inputs):
            result["reason"] = "EXTREME_INPUT"
        elif all(
            x
            and x.get("value") is not None
            and x.get("quality", x.get("status")) in ("GOOD", "OK")
            and 0 <= input_age(x, start + 1) <= rule.max_age
            for x in inputs
        ):
            ns = [x["value"] for x in inputs]
            try:
                metadata = {}
                match rule.operation:
                    case "thermal_stress":
                        model = next((m for m in config.models if m.id == rule.model_id), None)
                        if model is None:
                            raise ValueError("MODEL_NOT_CONFIGURED")
                        estimate = estimate_thermal(model, *ns)
                        value = estimate["value"]
                        metadata = {"estimate": estimate["estimate"]}
                    case "bridge_strain":
                        # Full bridge N=1, temporarily fixed gauge factor GF=6.
                        value = ns[0] * 1000 / 6
                    case "sum":
                        value = math.fsum(ns)
                    case "mean":
                        value = statistics.fmean(ns)
                    case "maximum":
                        value = max(ns)
                    case "difference":
                        value = ns[0] - ns[1]
                    case "ratio":
                        value = ns[0] / ns[1]
                    case "derivative" | "change":
                        prev = previous
                        if not prev or prev["run_id"] != frame["run_id"] or prev["window_end"] != start:
                            raise ValueError("NONCONTIGUOUS")
                        old = prev["values"].get(rule.inputs[0], prev["derived_results"].get(rule.inputs[0]))
                        if (
                            old is None
                            or old["value"] is None
                            or old.get("quality", old.get("status")) not in ("GOOD", "OK")
                            or old.get("extreme", False)
                            or not 0 <= input_age(old, prev["window_end"]) <= rule.max_age
                        ):
                            raise ValueError("PREVIOUS_UNAVAILABLE")
                        value = ns[0] - old["value"]
                        if rule.operation == "derivative":
                            value /= start - prev["window_start"]
                value = value * rule.scale + rule.offset
                if not math.isfinite(value):
                    raise ValueError("NONFINITE")
                result = {
                    "value": value,
                    "status": "OK",
                    "reason": None,
                    "max_input_age": max(input_age(x, start + 1) for x in inputs),
                    "extreme": is_extreme(value, rule),
                    **metadata,
                }
            except (ValueError, OverflowError, ZeroDivisionError) as error:
                result["reason"] = str(error)
        derived_results[rule.id] = result
    return {**frame, "derived_results": derived_results}
