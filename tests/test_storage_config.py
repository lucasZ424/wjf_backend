import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from htsg.cli import _run
from htsg.config import Settings, Storage, load_settings


def test_default_policy_keeps_records_until_growth_is_measured():
    policy = Storage()
    assert not policy.cleanup_enabled
    assert not policy.external_enabled
    assert policy.external_start == policy.emergency_start == 0.95
    assert policy.development_budget_bytes == 10 * 1024**3


def test_development_cap_can_be_lowered_but_not_exceed_authorized_limit():
    assert Storage(development_budget_bytes=1024).development_budget_bytes == 1024
    with pytest.raises(ValidationError):
        Storage(development_budget_bytes=10 * 1024**3 + 1)


def test_external_storage_needs_explicit_path_before_enabling():
    with pytest.raises(ValidationError, match="external_path"):
        Storage(external_enabled=True)


def test_development_external_database_stays_on_d():
    with pytest.raises(ValidationError, match="must be on D"):
        Settings(storage=Storage(external_enabled=True, external_path="C:/external"))
    config = Settings(storage=Storage(external_enabled=True, external_path="D:/external"))
    assert config.storage.external_enabled


def test_external_switch_precedes_or_coincides_with_emergency():
    with pytest.raises(ValidationError, match="external_start"):
        Storage(external_start=0.96)


@pytest.mark.parametrize(
    "path",
    [
        "config/development.json",
        "config/research-sim.json",
        "config/production.template.json",
    ],
)
def test_shipped_policy_pauses_cleanup_and_does_not_assume_drive_is_attached(path):
    policy = load_settings(path).storage
    assert not policy.cleanup_enabled and not policy.external_enabled
    assert policy.development_budget_bytes == 10 * 1024**3
    assert policy.external_start == 0.95


def test_cli_rejects_enabled_external_without_binding_before_starting(tmp_path):
    database = tmp_path / "database.json"
    database.write_text(json.dumps({"primary": "unused", "emergency": "unused"}))
    config = Settings(storage=Storage(external_enabled=True, external_path="D:/external"))
    with pytest.raises(SystemExit, match="external database binding"):
        _run(SimpleNamespace(database=str(database)), config)
