"""Lightweight operators over batch-end state (终版 §6).  Pure: no clock, no I/O, no await."""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from .config import Derived, Settings
from .extremes import is_extreme
from .records import RESULT_NA, RESULT_OK
from .thermal import estimate_thermal

GAUGE_FACTOR = 6  # §3.1: unified GF=6, full bridge N=1; not a per-sensor calibration
BRIDGE_N = 1


@dataclass(frozen=True, slots=True)
class Input:
    """A rule input as seen at the end of a batch."""

    value: float | None
    valid: bool
    extreme: bool = False


@dataclass(frozen=True, slots=True)
class Outcome:
    value: float | None
    status: str
    reason: str | None
    extreme: bool = False
    estimate: dict | None = None
    basis: dict | None = None  # strain_change: current and previous planned round


def unavailable(reason: str) -> Outcome:
    return Outcome(None, RESULT_NA, reason)


def compute(rule: Derived, values: list[float], models: dict) -> tuple[float, dict | None]:
    estimate = None
    match rule.operation:
        case "bridge_strain":
            value = values[0] * BRIDGE_N * 1000 / GAUGE_FACTOR
        case "sum":
            value = math.fsum(values)
        case "mean":
            value = statistics.fmean(values)
        case "maximum":
            value = max(values)  # signed maximum, never abs()
        case "absolute_peak":
            # 修复轮15: the largest magnitude with its sign kept; of equal magnitudes the first input in order
            value = max(values, key=abs)
        case "difference":
            value = values[0] - values[1]
        case "ratio":
            value = values[0] / values[1]
        case "thermal_stress":
            model = models.get(rule.model_id)
            if model is None:
                raise ValueError("MODEL_NOT_CONFIGURED")
            result = estimate_thermal(model, *values)
            value, estimate = result["value"], result["estimate"]
        case _:
            raise ValueError("UNSUPPORTED_OPERATION")
    value = value * rule.scale + rule.offset
    if not math.isfinite(value):
        raise ValueError("NONFINITE")
    return value, estimate


def evaluate(rule: Derived, inputs: list[Input], models: dict) -> Outcome:
    """One rule over its inputs.  Every input must be valid: nothing is borrowed from earlier state."""
    if rule.operation == "unconfigured":
        return unavailable("MODEL_NOT_CONFIGURED")
    if any(x.extreme for x in inputs):
        return unavailable("EXTREME_INPUT")
    if not all(x.valid and x.value is not None for x in inputs):
        return unavailable("INPUT_UNAVAILABLE")
    try:
        value, estimate = compute(rule, [x.value for x in inputs], models)
    except (ValueError, OverflowError, ZeroDivisionError) as error:
        return unavailable(str(error) or type(error).__name__)
    return Outcome(value, RESULT_OK, None, is_extreme(value, rule), estimate)


class Topology:
    """Dependency order of the derived rules; a batch only revisits rules downstream of what it touched."""

    def __init__(self, config: Settings):
        self.rules = tuple(config.derived)  # config validation guarantees topological order
        self.models = {m.id: m for m in config.models}
        self.by_id = {r.id: r for r in self.rules}
        self.downstream: dict[str, tuple[str, ...]] = {}
        reach: dict[str, set[str]] = {}
        for rule in self.rules:
            for name in rule.inputs:
                reach.setdefault(name, set()).add(rule.id)
        order = {r.id: n for n, r in enumerate(self.rules)}
        for name in reach:
            seen, stack = set(), [name]
            while stack:
                for nxt in reach.get(stack.pop(), ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            self.downstream[name] = tuple(sorted(seen, key=order.__getitem__))

    def affected(self, touched) -> list[Derived]:
        ids = set()
        for name in touched:
            ids.update(self.downstream.get(name, ()))
        return [r for r in self.rules if r.id in ids]
