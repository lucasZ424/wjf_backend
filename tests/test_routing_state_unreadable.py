"""An emergency database out of reach at start does not stop the normal one (01-3, 修复轮10; 终版 §13.5).

Why it matters: the routing state lives in the emergency database.  Unreadable, it used to leave the route UNKNOWN, and every
group waited on the emergency database - a sound normal database took nothing, the archive stood still after a restart.  Now
the normal database takes groups for the time being, but only while that is plainly safe: below the deletion start line,
with the group's bucket, after a write probe.  The emergency database is asked again only now and then, never before every
group.  Once it answers, the choice is made again from the disks as they are, never by the old mode it held.  Groups whose
first write went somewhere stay there.  The development mode keeps its own rule: an occupancy it cannot read refuses.
"""

import psycopg
import pytest
from test_archive_router import Disks, config
from test_archive_write import RUN, make_group

from htsg import archive_router
from htsg.archive import ArchiveStore
from htsg.archive_router import ArchiveRouter


def production(**storage):
    cfg = config(**storage)
    return type(cfg).model_validate({**cfg.model_dump(mode="json"), "mode": "production"})


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Reachable:
    """The emergency database's routing state, reachable or not, and how often it was asked."""

    def __init__(self, store):
        self.store, self.real, self.up, self.asked, self.saves_fail = store, store.routing_mode, False, 0, False
        store.routing_mode = self

    def __call__(self, value=None):
        self.asked += 1
        if not self.up or (value and self.saves_fail):
            raise psycopg.OperationalError("the emergency database does not answer")
        return self.real(value)


@pytest.fixture
def down(archive_db_factory):
    """A production router whose emergency database cannot be reached from the start."""

    def make(cfg=None):
        cfg = cfg or production()
        primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
        state = Reachable(emergency)
        disks, clock = Disks(), Clock()
        router = ArchiveRouter(cfg, primary, emergency, disk_usage=disks, clock=clock)
        router.initialize()
        router.bind_run(RUN, cfg.config_id, 999.0)
        return router, state, disks, clock

    return make


def stored(store, table="commit_group"):
    with store.session() as conn:
        return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def group(order, bucket=1, cfg=None):
    return make_group(order=order, config_id=(cfg or production()).config_id, bucket=bucket)


def test_with_the_routing_state_out_of_reach_a_sound_normal_database_takes_the_groups(down):
    router, state, disks, clock = down()
    assert router.mode == "UNKNOWN"
    result = router.write_group(group(1))
    assert (result.status, result.target) == ("COMMITTED", "primary"), result
    assert router.health["active_target"] == "PRIMARY_PROVISIONAL", "said to be for the time being, not checked"
    assert stored(router.primary) == 1
    # 修复轮12: the mode reads PRIMARY now, and the pages showed "正常库"; what they read says it is unchecked
    assert router.mode == "PRIMARY" and router.target == "PRIMARY_PROVISIONAL"


def test_the_development_mode_keeps_refusing_when_it_cannot_read_what_the_databases_hold(archive_db_factory):
    cfg = config()
    gone = ArchiveStore("host=127.0.0.1 port=1 dbname=none user=none connect_timeout=2")  # nothing listens there
    router = ArchiveRouter(cfg, archive_db_factory("primary"), gone, disk_usage=Disks())
    router.initialize()
    router.bind_run(RUN, cfg.config_id, 999.0)
    result = router.write_group(group(1, cfg=cfg))
    assert (result.status, result.reason) == ("REFUSED", "DEVELOPMENT_BUDGET_UNKNOWN")


@pytest.mark.parametrize("why", ["no bucket", "probe fails", "above the start line"])
def test_without_the_bucket_a_write_probe_or_room_below_the_start_line_the_group_waits(down, why):
    router, state, disks, clock = down()
    bucket = 1
    if why == "no bucket":
        bucket = 7  # not prepared in the normal database
    elif why == "probe fails":
        router.primary.probe_write = lambda: (_ for _ in ()).throw(psycopg.OperationalError("probe"))
    else:
        disks.used["D:/a"] = 0.92
    result = router.write_group(group(1, bucket=bucket))
    assert result.status == "FAILED" and result.target is None, "it waits, chosen nowhere yet"
    assert group(1, bucket=bucket).group_id not in router.pinned and stored(router.primary) == 0
    assert router.target == "UNKNOWN", "nothing goes anywhere: the pages must not name a database"


def test_writing_to_the_normal_database_for_the_time_being_stops_at_the_start_line(down):
    router, state, disks, clock = down()
    assert router.write_group(group(1)).target == "primary"
    disks.used["D:/a"] = 0.91
    result = router.write_group(group(2))
    assert result.status == "FAILED" and result.target is None and stored(router.primary) == 1
    # 修复轮13: the mode still reads PRIMARY and the route provisional, but nothing is written - the pages must say so
    assert router.target == "UNKNOWN", "waiting after a provisional stretch is not writing to the normal database"
    disks.used["D:/a"] = 0.50
    assert router.write_group(group(3)).target == "primary"
    assert router.target == "PRIMARY_PROVISIONAL", "writing again: for the time being, as before"


def test_the_emergency_database_is_asked_now_and_then_not_before_every_group(down):
    router, state, disks, clock = down()
    before = state.asked
    for n in range(1, 11):
        assert router.write_group(group(n)).durable
    assert state.asked - before <= 1, "ten groups did not each wait on the emergency database"
    clock.now += archive_router.STATE_RETRY + 1
    assert router.write_group(group(11)).durable
    assert state.asked - before <= 2


def test_once_the_emergency_database_answers_the_route_is_chosen_from_the_disks_as_they_are(down):
    router, state, disks, clock = down()
    state.up = True
    state("EMERGENCY")  # the mode it held from before: the normal disk was full then
    state.up = False
    assert router.write_group(group(1)).target == "primary"
    state.up = True
    clock.now += archive_router.STATE_RETRY + 1
    assert router.write_group(group(2)).target == "primary", "the old mode did not override the choice made now"
    assert state.real() == "PRIMARY" and router.health["active_target"] == "PRIMARY" and router.target == "PRIMARY"


def test_a_routing_state_that_cannot_be_saved_on_recovery_keeps_the_route_provisional_and_the_writes_going(down):
    router, state, disks, clock = down()
    state.up = True
    state("EMERGENCY")
    state.up, state.saves_fail = True, True  # it can be read again, but nothing can be saved in it
    clock.now += archive_router.STATE_RETRY + 1
    result = router.write_group(group(1))
    assert (result.status, result.target) == ("COMMITTED", "primary")
    assert router.health["active_target"] == "PRIMARY_PROVISIONAL", "not checked: still for the time being"
    assert router.target == "PRIMARY_PROVISIONAL", "the state held EMERGENCY: the pages must not say the emergency database"


def test_a_failed_write_keeps_its_group_where_it_went_and_the_next_new_group_probes_again(down):
    router, state, disks, clock = down()
    probes = []
    real_probe = router.primary.probe_write
    router.primary.probe_write = lambda: (probes.append(1), real_probe())[1]
    real_write = router.primary.write_group
    fail = {"once": True}

    def flaky(g):
        if fail.pop("once", False):
            return archive_router.WriteOutcome("FAILED", "OperationalError")
        return real_write(g)

    router.primary.write_group = flaky
    assert router.write_group(group(1)).status == "FAILED"
    assert router.pinned[group(1).group_id] == "primary", "its first write went to the normal database: it stays there"
    assert len(probes) == 1
    assert router.write_group(group(2)).durable
    assert len(probes) == 2, "a write that failed makes the probe stale: the next new group probes again"
    state.up = True
    state("EMERGENCY")
    disks.used["D:/a"] = 0.96  # the normal disk is full now, the emergency database answers
    clock.now += archive_router.STATE_RETRY + 1
    retried = router.write_group(group(1))
    assert retried.target == "primary", "a group already given a database is retried there, whatever the route is now"
