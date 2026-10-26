"""D26: route recovery, budgets and maintenance (终版 §13.4-13.5).

Real PostgreSQL databases; disk readings are injected (no real drive is ever filled).  Why it matters:
routing decides which database holds each group; a wrong switch, a silent quota bypass or a deleting
maintenance step would lose or hide history.
"""

import asyncio
from uuid import uuid4

import psycopg
import pytest
from owner_support import small_config
from pydantic import ValidationError
from test_archive_router import Disks, config
from test_archive_write import make_group

from htsg.archive import ArchiveStore, WriteOutcome
from htsg.archive_router import ArchiveRouter
from htsg.commitq import CommitQueue
from htsg.config import Settings

GIB = 1024**3


class Rig3:
    """Normal, emergency and removable databases, all throw-away."""

    def __init__(self, factory, **storage):
        self.disks = Disks()
        self.cfg = config(external_enabled=True, **storage)
        self.primary, self.emergency = factory("primary"), factory("emergency")
        self.external = factory("primary")  # another database, so it is distinct from the normal one
        self.router = self.new_router()
        self.router.initialize()
        self.router.bind_run("run1", self.cfg.config_id, 999.0)
        self.n = 0

    def new_router(self):
        return ArchiveRouter(self.cfg, self.primary, self.emergency, self.external, disk_usage=self.disks)

    def write(self, primary=None, external=None):
        if primary is not None:
            self.disks.used["D:/a"] = primary
        if external is not None:
            self.disks.used["D:/c"] = external
        self.n += 1
        return self.router.write_group(make_group(order=self.n, config_id=self.cfg.config_id))

    def where(self):
        found = {}
        for name, store in (("primary", self.primary), ("emergency", self.emergency), ("external", self.external)):
            with store.session() as conn:
                for row in conn.execute("SELECT group_id FROM commit_group").fetchall():
                    found.setdefault(row["group_id"], []).append(name)
        return found

    def rows(self, name):
        with getattr(self, name).session() as conn:
            return conn.execute("SELECT count(*) AS n FROM observation").fetchone()["n"]


@pytest.fixture
def rig(archive_db_factory):
    return Rig3(archive_db_factory)


def test_at_95_percent_new_groups_go_to_the_removable_database_old_data_stays_where_it_is(rig):
    assert rig.write(primary=0.50).target == "primary"
    assert rig.write(primary=0.96, external=0.20).target == "external"
    assert rig.write(primary=0.30).target == "external", "once selected, space freeing up does not switch back"
    assert rig.router.mode == "EXTERNAL" and rig.router.external_selected
    assert rig.where() == {"run1/g1": ["primary"], "run1/g2": ["external"], "run1/g3": ["external"]}
    assert rig.rows("primary") == 3 and rig.rows("external") == 6, "nothing was copied or removed on the switch"
    assert rig.emergency.routing_mode() == "EXTERNAL" and rig.emergency.external_selected()


def test_the_selection_survives_a_restart_and_a_recovered_primary(rig):
    rig.write(primary=0.96, external=0.20)
    restarted = rig.new_router()
    restarted.initialize()
    restarted.bind_run("run1", rig.cfg.config_id, 999.0)
    rig.router = restarted
    assert rig.write(primary=0.10).target == "external"


def test_when_the_removable_database_fails_the_emergency_route_takes_over_and_recovery_returns_only_to_it(rig):
    rig.write(primary=0.96, external=0.20)
    real = rig.disks.__call__

    def external_unreadable(path):
        if path == "D:/c":
            raise OSError("unplugged")
        return real(path)

    rig.router.disk_usage = external_unreadable
    assert rig.write(primary=0.10).target == "emergency", "removable volume unreadable -> emergency"
    rig.router.disk_usage = real
    assert rig.write(primary=0.10, external=0.80).target == "emergency", "80 % is still above the 70 % line"
    assert rig.write(primary=0.10, external=0.60).target == "external", "back to the removable one, never primary"
    where = rig.where()
    assert where["run1/g1"] == ["external"] and all("primary" not in v for v in where.values())


def test_without_a_removable_database_emergency_ends_only_at_70_percent_and_with_a_successful_probe(archive_db_factory):
    disks = Disks()
    cfg = config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    router = ArchiveRouter(cfg, primary, emergency, disk_usage=disks)
    router.initialize()
    router.bind_run("run1", cfg.config_id, 999.0)
    order = iter(range(1, 100))

    def write(used):
        disks.used["D:/a"] = used
        return router.write_group(make_group(order=next(order), config_id=cfg.config_id)).target

    assert write(0.96) == "emergency"
    assert write(0.80) == "emergency", "hysteresis: recovery needs 70 %, not merely less than 95 %"
    real_probe = primary.probe_write

    def probe_fails():
        raise psycopg.OperationalError("read-only")

    primary.probe_write = probe_fails
    assert write(0.60) == "emergency" and router.health["switchback"] == "OperationalError"
    primary.probe_write = real_probe
    assert write(0.60) == "primary" and router.health["switchback"] == "OK"


def test_a_removable_database_that_fails_the_binding_check_is_never_written_or_probed(archive_db_factory):
    disks = Disks()
    cfg = config(external_enabled=True)
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    same_as_primary = ArchiveStore(primary.dsn)  # NOT a distinct database: the binding must be refused
    probes = []
    same_as_primary.probe_write = lambda: probes.append(1)
    router = ArchiveRouter(cfg, primary, emergency, same_as_primary, disk_usage=disks)
    router.initialize()
    router.bind_run("run1", cfg.config_id, 999.0)
    disks.used["D:/a"] = 0.96
    result = router.write_group(make_group(config_id=cfg.config_id))
    assert result.target == "emergency" and probes == [] and router.health["external"] == "ExternalBindingError"


def test_production_validates_the_volume_and_still_keeps_reserve_batch_budget_and_gap(archive_db_factory):
    payload = small_config().model_dump(mode="json")
    payload.update(mode="production", dev_admin_login=False)
    payload["storage"] = {
        "primary_path": "E:/a", "emergency_path": "E:/b", "external_enabled": True, "external_path": "E:/c",
    }  # fmt: skip
    cfg = Settings.model_validate(payload)
    with pytest.raises(ValidationError, match="dev_admin_login"):
        Settings.model_validate({**payload, "dev_admin_login": True})
    disks = Disks()
    primary, emergency, external = (archive_db_factory(i) for i in ("primary", "emergency", "primary"))
    router = ArchiveRouter(cfg, primary, emergency, external, disk_usage=disks)
    router.initialize()
    router.bind_run("run1", cfg.config_id, 999.0)
    router.bytes_written = 50 * GIB  # far beyond the 10 GiB development quota
    assert router.write_group(make_group(config_id=cfg.config_id)).target == "primary", "quota is lifted"
    disks.used["E:/a"] = 0.96  # volume of the real removable database is not E: -> binding refused
    result = router.write_group(make_group(order=2, config_id=cfg.config_id))
    assert result.target == "emergency" and router.health["external"] == "ExternalBindingError"
    disks.used["E:/b"] = 0.95  # emergency volume: 50 GiB of 1000 free < the 100 GiB reserve
    refused = router.write_group(make_group(order=3, config_id=cfg.config_id))
    assert (refused.status, refused.reason) == ("REFUSED", "EMERGENCY_RESERVE")


def test_development_quota_and_reserve_refuse_the_whole_group(archive_db_factory):
    payload = config().model_dump(mode="json")
    payload["storage"]["development_budget_bytes"] = 1024
    cfg = Settings.model_validate(payload)
    disks = Disks()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    router = ArchiveRouter(cfg, primary, emergency, disk_usage=disks)
    router.initialize()
    router.bind_run("run1", cfg.config_id, 999.0)
    result = router.write_group(make_group(config_id=cfg.config_id))
    assert (result.status, result.reason) == ("REFUSED", "DEVELOPMENT_WRITE_BUDGET_EXHAUSTED")
    disks.used["D:/a"] = 0.9995  # less than 1 GiB left on the volume
    router.config = config()
    assert router.write_group(make_group(order=2, config_id=cfg.config_id)).reason == "DEVELOPMENT_DISK_RESERVE"
    with primary.session() as conn:
        assert conn.execute("SELECT count(*) AS n FROM commit_group").fetchone()["n"] == 0


def test_unknown_development_occupancy_refuses_and_a_known_one_is_kept(archive_db_factory):
    cfg = config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    router = ArchiveRouter(cfg, primary, emergency, disk_usage=Disks())
    router.initialize()
    router.bind_run("run1", cfg.config_id, 999.0)

    class Broken:
        def __enter__(self):
            raise psycopg.OperationalError("down")

        def __exit__(self, *a):
            return False

    real = emergency.session
    emergency.session = lambda: Broken()
    result = router.write_group(make_group(config_id=cfg.config_id))
    assert (result.status, result.reason) == ("REFUSED", "DEVELOPMENT_BUDGET_UNKNOWN")
    emergency.session = real
    assert router.write_group(make_group(order=2, config_id=cfg.config_id)).durable
    router.last_budget_check = -1e9
    emergency.session = lambda: Broken()
    assert router.write_group(make_group(order=3, config_id=cfg.config_id)).durable, "the last observation is kept"
    assert router.health["emergency_budget"] == "CACHED"


def test_heavy_statistics_every_60_seconds_light_status_every_second(archive_db_factory):
    cfg = config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    router = ArchiveRouter(cfg, primary, emergency, disk_usage=Disks())
    router.initialize()
    seen = [(t, router.maintain(t)) for t in (0.0, 0.5, 1.0, 30.0, 59.9, 60.0, 60.5, 120.0)]
    flags = {t: (d["light"], d["heavy"]) for t, d in seen}
    assert flags == {0.0: (True, True), 0.5: (False, False), 1.0: (True, False), 30.0: (True, False),
                     59.9: (True, False), 60.0: (False, True), 60.5: (False, False), 120.0: (True, True)}  # fmt: skip
    assert router.health["primary_metrics"]["bytes"] > 0


@pytest.mark.parametrize("cleanup_enabled", [False, True])
def test_maintenance_never_deletes_records_older_than_a_year(archive_db_factory, cleanup_enabled):
    cfg = config(cleanup_enabled=cleanup_enabled)
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    router = ArchiveRouter(cfg, primary, emergency, disk_usage=Disks())
    router.initialize()
    router.bind_run("run1", cfg.config_id, 999.0)
    group = make_group(config_id=cfg.config_id)
    assert router.write_group(group).durable
    with primary.session() as conn:
        conn.execute("UPDATE observation SET received_wall=received_wall-400*86400,received_mono=1")
        conn.execute("UPDATE result SET at_wall=at_wall-400*86400")
        conn.execute("UPDATE commit_group SET committed_at=now()-interval '400 days'")
        before = {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in ("observation", "result", "commit_group")}
    for t in (0.0, 61.0, 122.0, 600.0):
        router.maintain(t)
    with primary.session() as conn:
        after = {t: conn.execute(f"SELECT count(*) AS n FROM {t}").fetchone()["n"] for t in before}
    assert after == before and before["observation"] == 3, "more than a year old and still there"


@pytest.mark.parametrize("bad", [0, -1, 1023, 10 * GIB + 1, None, 2**63])
def test_a_development_budget_that_would_bypass_the_quota_is_rejected(bad):
    with pytest.raises(ValidationError):
        Settings.model_validate({"sources": [{"id": "s", "kind": "modbus"}], "storage": {"development_budget_bytes": bad}})


@pytest.mark.parametrize("good", [1024, 10 * GIB, 5 * GIB])
def test_the_boundary_budgets_are_valid(good):
    cfg = Settings.model_validate({"sources": [{"id": "s", "kind": "modbus"}], "storage": {"development_budget_bytes": good}})
    assert cfg.storage.development_budget_bytes == good


# ------------------------------------------------- storage paths that cannot be read (review 2026-10-26, no. 1)


class Unreadable(Disks):
    """Disk readings in which some paths cannot be read at all: the disk is gone, its letter changed, the folder was removed."""

    def __init__(self):
        super().__init__()
        self.missing = set()

    def __call__(self, path):
        if path in self.missing:
            raise FileNotFoundError(f"[WinError 3] the system cannot find the path specified: {path}")
        return super().__call__(path)


def two_databases(archive_db_factory, disks, **storage):
    cfg = config(**storage)
    router = ArchiveRouter(cfg, archive_db_factory("primary"), archive_db_factory("emergency"), disk_usage=disks)
    router.initialize()
    router.bind_run("run1", cfg.config_id, 999.0)
    return router, cfg


def committed(store):
    with store.session() as conn:
        return [row["group_id"] for row in conn.execute("SELECT group_id FROM commit_group ORDER BY order_no").fetchall()]


def test_a_primary_volume_that_cannot_be_read_holds_the_group_back_and_the_same_group_is_written_once_it_can(archive_db_factory):
    """An unreadable storage path used to raise out of the writer and end the process, and on restart the first write raised
    again.  Without the primary's reading the 95 % decision cannot be made, so the group waits: FAILED is retried with the same
    group and the same ids - nothing is guessed, nothing is written elsewhere, and nothing is lost while it lasts."""
    disks = Unreadable()
    router, cfg = two_databases(archive_db_factory, disks)
    disks.missing.add("D:/a")
    group = make_group(config_id=cfg.config_id)
    result = router.write_group(group)
    assert (result.status, result.reason) == ("FAILED", "PRIMARY_DISK_UNAVAILABLE")
    assert router.health["primary_disk"] == "UNAVAILABLE"
    assert committed(router.primary) == [] and committed(router.emergency) == []
    disks.missing.clear()
    assert router.write_group(group).target == "primary"
    assert "primary_disk" not in router.health, "a readable volume clears the report"


def test_an_unreadable_emergency_volume_matters_only_when_the_emergency_route_is_needed(archive_db_factory):
    disks = Unreadable()
    router, cfg = two_databases(archive_db_factory, disks)
    disks.missing.add("D:/b")
    assert router.write_group(make_group(order=1, config_id=cfg.config_id)).target == "primary", "the primary has room"
    disks.used["D:/a"] = 0.96
    waiting = make_group(order=2, config_id=cfg.config_id)
    result = router.write_group(waiting)
    assert (result.status, result.reason) == ("FAILED", "EMERGENCY_DISK_UNAVAILABLE"), "its reserve cannot be checked: wait"
    assert router.health["emergency_disk"] == "UNAVAILABLE"
    disks.missing.clear()
    assert router.write_group(waiting).target == "emergency"
    assert committed(router.emergency) == [waiting.group_id]


def test_a_failed_removable_write_does_not_fall_back_blind_when_the_emergency_volume_cannot_be_read(rig):
    assert rig.write(primary=0.96, external=0.20).target == "external"
    disks = Unreadable()
    disks.used = rig.disks.used
    rig.router.disk_usage = disks
    disks.missing.add("D:/b")
    rig.external.write_group = lambda group: WriteOutcome("FAILED", "OperationalError")
    result = rig.write()
    assert (result.status, result.reason) == ("FAILED", "EMERGENCY_DISK_UNAVAILABLE")


def test_a_failed_removable_write_with_an_unreachable_emergency_database_keeps_the_group_and_the_writer(rig):
    """In the removable-disk mode a failed write falls back to the emergency database.  When that database could not be reached
    either, recording the switch raised out of the writer and ended the process (review 2026-10-26, no. 2).  Now the group waits
    and the switch is made only once it is recorded; the same group goes to the emergency database when it answers."""
    assert rig.write(primary=0.96, external=0.20).target == "external"
    rig.external.write_group = lambda group: WriteOutcome("FAILED", "OperationalError")
    recorded = rig.emergency.routing_mode

    def unreachable(value=None):
        raise psycopg.OperationalError("connection refused")

    rig.emergency.routing_mode = unreachable
    group = make_group(order=99, config_id=rig.cfg.config_id)
    result = rig.router.write_group(group)
    assert (result.status, result.reason) == ("FAILED", "OperationalError")
    assert rig.router.mode == "EXTERNAL", "the switch is not made while it cannot be recorded"
    rig.emergency.routing_mode = recorded
    assert rig.router.write_group(group).target == "emergency", "the same group, once the emergency database answers"
    assert rig.where()[group.group_id] == ["emergency"]


async def test_the_writer_keeps_running_when_a_configured_storage_path_does_not_exist(archive_db_factory):
    """The real disk reading, no injection, on a path that is not there: the writer task stays and keeps the group."""
    cfg = config(primary_path="D:/htsg-missing-" + uuid4().hex + "/primary")
    router = ArchiveRouter(cfg, archive_db_factory("primary"), archive_db_factory("emergency"))
    router.initialize()
    router.bind_run("run1", cfg.config_id, 999.0)
    queue = CommitQueue(router, 10, retry_delay=(0.05, 0.1))
    queue.offer(make_group(config_id=cfg.config_id))
    task = asyncio.create_task(queue.run())
    try:
        await asyncio.sleep(0.5)
        assert not task.done(), "the writer is still there"
        assert queue.stats["failures"] >= 2 and queue.stats["last_error"] == "PRIMARY_DISK_UNAVAILABLE"
        assert queue.depth == 1, "the group waits at the head of the queue"
    finally:
        await queue.stop(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
