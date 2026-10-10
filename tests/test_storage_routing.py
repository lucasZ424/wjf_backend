"""D26: route recovery, budgets and maintenance (终版 §13.4-13.5).

Real PostgreSQL databases; disk readings are injected (no real drive is ever filled).  Why it matters:
routing decides which database holds each group; a wrong switch, a silent quota bypass or a deleting
maintenance step would lose or hide history.
"""

import psycopg
import pytest
from owner_support import small_config
from pydantic import ValidationError
from test_archive_router import Disks, config
from test_archive_write import make_group

from htsg.archive import ArchiveStore
from htsg.archive_router import ArchiveRouter
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
        Settings.model_validate({"sources": [{"id": "s", "kind": "simulator"}], "storage": {"development_budget_bytes": bad}})


@pytest.mark.parametrize("good", [1024, 10 * GIB, 5 * GIB])
def test_the_boundary_budgets_are_valid(good):
    cfg = Settings.model_validate({"sources": [{"id": "s", "kind": "simulator"}], "storage": {"development_budget_bytes": good}})
    assert cfg.storage.development_budget_bytes == good
