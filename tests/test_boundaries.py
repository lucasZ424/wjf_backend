import time
from collections import namedtuple

import pytest

from htsg.auth import Accounts
from htsg.runtime import Runtime
from htsg.storage import Router, Store


def test_failed_account_persistence_does_not_activate_new_credential(tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    accounts.create("admin", "admin")

    def fail(users):
        raise OSError("disk failure")

    accounts.persist = fail
    with pytest.raises(OSError):
        accounts.create("new", "viewer")
    assert "new" not in accounts.users


def test_ingress_is_bounded_and_loss_visible(settings, databases):
    config = settings.model_copy(update={"limits": settings.limits.model_copy(update={"ingress": 1})})
    runtime = Runtime(config, Router(config, *databases))
    assert runtime.submit("x", 1)
    assert not runtime.submit("x", 2)
    assert runtime.health["ingress_dropped"] == 1
    runtime.io.shutdown()
    runtime.read_io.shutdown()
    runtime.calc_io.shutdown()


def test_actual_emergency_writes_only_source_and_rejects_results(settings, databases, monkeypatch):
    import shutil

    usage = namedtuple("usage", "total used free")
    total = 100 * 1024**3

    def fake_disk(path):
        return usage(total, int(total * 0.96), int(total * 0.04))

    monkeypatch.setattr(shutil, "disk_usage", fake_disk)
    router = Router(settings, *databases)
    router.initialize()
    router.write(
        "sample",
        {"event_id": "e", "sample": {"id": "s", "point_id": "x", "effective_time": time.time(), "value": 10}},
    )
    assert not databases[0].query("samples", event_id="e")
    assert len(databases[1].query("samples", event_id="e")) == 1
    with pytest.raises(RuntimeError, match="RESULT_ARCHIVE_SUSPENDED"):
        router.write("frame", {"frame": {}, "event_ids": ["e"]})


def test_partial_query_reports_unavailable_instance(settings, databases):
    unavailable = Store("postgresql://invalid:invalid@127.0.0.1:1/absent")
    result = Router(settings, databases[0], unavailable).merged("events")
    assert result["partial"] and result["unavailable_instances"] == ["emergency"]
