
import pytest
from pydantic import ValidationError

from htsg.config import Storage, load_settings


def test_default_policy_keeps_records_until_growth_is_measured():
    policy = Storage()
    assert not policy.cleanup_enabled
    assert policy.emergency_start == 0.95
    assert policy.development_budget_bytes == 10 * 1024**3


def test_development_cap_can_be_lowered_but_not_exceed_authorized_limit():
    assert Storage(development_budget_bytes=1024).development_budget_bytes == 1024
    with pytest.raises(ValidationError):
        Storage(development_budget_bytes=10 * 1024**3 + 1)


@pytest.mark.parametrize(
    "path",
    [
        "config/development.json",
        "config/research-sim.json",
        "config/production.template.json",
        "config/production.example.json",
    ],
)
def test_shipped_policy_keeps_the_development_cap_and_does_not_assume_drive_is_attached(path):
    """Which of them start new deletions: test_rolling_config.py (修复轮09)."""
    policy = load_settings(path).storage
    assert policy.development_budget_bytes == 10 * 1024**3
