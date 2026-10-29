"""Configuration of the rolling deletion (修复轮09, 终版 §13.4, §13.6, §18.1).

Why it matters: the site keeps its normal database from filling by deleting whole buckets from the oldest one on.  The lines
are deployment values: deletion starts at 90 %, stops at the stop line (80 % unless the site writes another band), and the
same 90 % is where the emergency route ends.  A floor of buckets is always kept.  The switch starts new deletions in a
production deployment only - a development or research database is never thinned out by itself.
"""

import pytest
from pydantic import ValidationError

from htsg.config import Storage, load_settings


def test_deletion_starts_at_90_percent_stops_at_80_and_keeps_14_buckets_unless_the_site_says_otherwise():
    policy = Storage()
    assert (policy.cleanup_start, policy.cleanup_stop, policy.retention_min_buckets) == (0.90, 0.80, 14)
    assert policy.cleanup_stop < policy.cleanup_start < policy.emergency_start == 0.95


@pytest.mark.parametrize("value", [0, -1, 1.5, "14"])
def test_the_bucket_floor_is_a_whole_positive_number(value):
    with pytest.raises(ValidationError, match="retention_min_buckets") as refused:
        Storage(retention_min_buckets=value)
    assert "Extra inputs" not in str(refused.value), "refused for its value, not as an unknown field"


@pytest.mark.parametrize("band", [0.80, 0.65, 0.50])
def test_every_recommended_stop_band_is_accepted(band):
    assert Storage(cleanup_stop=band).cleanup_stop == band


@pytest.mark.parametrize(
    ("path", "enabled"),
    [
        ("config/production.template.json", True),
        ("config/production.example.json", True),
        ("config/development.json", False),
        ("config/research-sim.json", False),
    ],
)
def test_only_the_production_configurations_start_new_deletions(path, enabled):
    policy = load_settings(path).storage
    assert policy.cleanup_enabled is enabled
    if enabled:
        assert policy.cleanup_stop == 0.80, "the stop line is written out: it is the value the site decides at deployment"
