"""A point's `inclusive` flag is removed from the configuration (user ruling 2026-10-28).

Why it matters: nothing read it - a fault starts strictly beyond a bound and recovers at the hysteresis line
whatever it said - so it told whoever edits the site configuration that it changed the bound.  Like the removed
history windows (修复轮07), a configuration still carrying it is refused by name and never silently ignored.
"""

import pytest
from pydantic import ValidationError

from htsg.config import Settings

BASE = {"sources": [{"id": "s", "kind": "modbus"}], "derived": []}


def test_a_point_still_carrying_inclusive_is_refused_by_name():
    point = {"id": "p", "source": "s", "unit": "°C", "high": 10, "inclusive": True}
    with pytest.raises(ValidationError) as refused:
        Settings.model_validate({**BASE, "points": [point]})
    assert "points.0.inclusive" in str(refused.value) and "Extra inputs are not permitted" in str(refused.value)
