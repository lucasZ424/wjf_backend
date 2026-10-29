"""The removable-disk database is removed (修复轮08, user ruling 2026-10-28).

Why it matters: the site keeps its normal database from filling by deleting the oldest data, so a third database nobody
uses there is only branches that can go wrong there.  Like other removed settings, a configuration, a connection file or a
deployment file that still names it is refused by name before anything starts, never silently ignored; and a database an
earlier version left routed to the removable one is not stuck there.
"""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from test_archive_router import Disks, config
from test_archive_write import make_group

from htsg.archive_router import ArchiveRouter
from htsg.cli import _run
from htsg.config import Settings
from scripts import deploy_databases as deploy


@pytest.mark.parametrize(
    ("field", "value"), [("external_enabled", False), ("external_path", "E:/HTSG/external"), ("external_start", 0.95)]
)
def test_a_configuration_still_carrying_a_removable_disk_field_is_refused_by_name(field, value):
    with pytest.raises(ValidationError) as refused:
        Settings.model_validate({"storage": {field: value}})
    assert f"storage.{field}" in str(refused.value) and "Extra inputs are not permitted" in str(refused.value)


def test_a_connection_file_still_naming_a_removable_database_is_refused_before_anything_starts(tmp_path):
    database = tmp_path / "database.json"
    database.write_text(json.dumps({"primary": "unused", "emergency": "unused", "external": "unused"}))
    with pytest.raises(SystemExit, match="external"):
        _run(SimpleNamespace(database=str(database)), Settings())


def test_a_deployment_file_still_naming_a_removable_volume_is_refused(tmp_path):
    path = tmp_path / "deploy.json"
    path.write_text(json.dumps({"volumes": {"primary": "D:", "emergency": "C:", "external": "E:"}, "pg_bin": "x", "config": "y"}))
    with pytest.raises(SystemExit, match="external"):
        deploy.read_input(path)


def test_a_database_an_earlier_version_left_on_the_removable_route_writes_to_emergency_and_returns_by_the_normal_rule(
    archive_db_factory,
):
    disks, cfg = Disks(), config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    emergency.initialize(cfg)
    emergency.routing_mode("EXTERNAL")  # what the removable route persisted before 修复轮08
    router = ArchiveRouter(cfg, primary, emergency, disk_usage=disks)
    router.initialize()
    router.bind_run("run1", cfg.config_id, 999.0)
    order = iter(range(1, 100))

    def write(used):
        disks.used["D:/a"] = used
        return router.write_group(make_group(order=next(order), config_id=cfg.config_id)).target

    assert write(0.92) == "emergency", "read as the emergency route: primary was full when it was chosen"
    assert write(0.60) == "primary", "and it ends the way emergency always ends"
    assert emergency.routing_mode() == "PRIMARY"


def test_the_removable_route_an_earlier_version_left_is_already_the_emergency_route_at_start_up(archive_db_factory):
    """The self-check page shows the route from start-up on, before any group is written; a database whose sources are all
    down writes none, and it must not show a route that no longer exists (audit 2026-10-29, no. 08-3)."""
    cfg = config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    emergency.initialize(cfg)
    emergency.routing_mode("EXTERNAL")  # what the removable route persisted before 修复轮08
    router = ArchiveRouter(cfg, primary, emergency, disk_usage=Disks())
    router.initialize()
    assert router.mode == "EMERGENCY", "read as the emergency route as soon as it is read"
    assert emergency.routing_mode() == "EMERGENCY", "and recorded so: the next start reads a route that exists"
