import json
from contextlib import nullcontext
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


def test_a_development_removable_database_may_be_on_another_drive():
    """修复轮06: the rehearsal's removable database is on E:; the configuration takes it in development too."""
    config = Settings(storage=Storage(external_enabled=True, external_path="E:/external"))
    assert config.storage.external_path == "E:/external"


def test_the_removable_binding_check_still_wants_its_own_volume_but_no_longer_d(monkeypatch):
    """修复轮06: lifting "only on D:" leaves the rest of the check: the data must really be on the configured volume
    and must not be one of the other databases."""
    from htsg import archive_router

    class Store:
        def __init__(self, role):
            self.role, self._local = role, SimpleNamespace()

        def session(self):
            return nullcontext(self)

    places = {"external": r"e:\htsg\external\pgdata", "primary": r"d:\htsg\primary\pgdata", "emergency": r"c:\htsg\emergency\pgdata"}
    monkeypatch.setattr(archive_router, "database_identity", lambda store: (places[store.role], "htsg"))
    storage = {"primary_path": "D:/HTSG/primary", "emergency_path": "C:/HTSG/emergency", "external_enabled": True}
    config = Settings(storage=Storage(**storage, external_path="E:/HTSG/external"))
    archive_router.validate_external_binding(Store("external"), config, Store("primary"), Store("emergency"))
    elsewhere = Settings(storage=Storage(**storage, external_path="F:/HTSG/external"))
    with pytest.raises(archive_router.ExternalBindingError, match="configured volume"):
        archive_router.validate_external_binding(Store("external"), elsewhere, Store("primary"), Store("emergency"))


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
