"""The site fills its configuration by copying `config/production.example.json` (the template stays empty).

So the example must stay a valid production configuration, must stay a complete mapping once its sources are
switched on (a disabled source skips the mapping checks, which would hide a missing address or NodeId), and must
show every point field that changes behaviour - thresholds, hysteresis and extremes above all - or the site has
nothing to copy them from.
"""

import json
from pathlib import Path

from htsg.config import Point, Settings, load_settings

EXAMPLE = Path(__file__).resolve().parents[1] / "config/production.example.json"
IDENTITY = {"id", "source", "unit"}
BANDS = {"low", "high", "hysteresis_low", "hysteresis_high", "extreme_low", "extreme_high"}


def raw():
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def test_example_is_a_valid_production_configuration_that_acquires_nothing():
    config = load_settings(EXAMPLE)
    assert config.mode == "production"
    assert not any(s.enabled for s in config.sources)  # a copied example must not poll made-up addresses


def test_example_mapping_is_complete_once_its_sources_are_switched_on():
    data = raw()
    for source in data["sources"]:
        source["enabled"] = True
    Settings.model_validate(data)


def test_example_writes_out_every_point_field_that_changes_behaviour():
    written = {key for point in raw()["points"] for key in point}
    assert set(Point.model_fields) - IDENTITY - written == set()


def test_example_shows_thresholds_and_extremes_on_a_derived_quantity_too():
    written = {key for derived in raw()["derived"] for key in derived}
    assert BANDS - written == set()
