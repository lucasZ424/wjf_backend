"""Explicit engineering limits; no dead-point, jump or adaptive outlier detection."""


def is_extreme(value, definition):
    if value is None:
        return False
    low, high = definition.extreme_low, definition.extreme_high
    return (low is not None and value < low) or (high is not None and value > high)
