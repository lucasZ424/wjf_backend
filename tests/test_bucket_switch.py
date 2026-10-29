"""Which bucket a group is put in, when the next one is taken, and where a group goes when its bucket is missing
(修复轮09 09d, 终版 §13.5, §13.6 准备与换桶, plan D3, D4, D7).

Why it matters: a bucket must hold at least twelve hours of the process really running and end at a change of date, so the
floor of fourteen buckets keeps at least seven days of run time - whatever the wall clock did.  The twelve hours count only
once they are on disk: a crash must never make a bucket look older than it is.  The state owner never waits: when the next
bucket is not ready the current one is used longer, and when no bucket is ready at all the groups are refused as a gap and
acquisition goes on.  A group whose bucket the normal database lacks goes to the emergency one whole, its bucket unchanged.
"""

import asyncio
import json
import threading
import time
from types import MappingProxyType, SimpleNamespace

import pytest
from owner_support import small_config
from test_archive_router import Disks, config
from test_archive_write import make_group

from htsg import buckets as b
from htsg.archive_router import ArchiveRouter
from htsg.runtime import Runtime

HOUR = 3600.0


def snap(primary=(), emergency=()):
    return b.Snapshot(MappingProxyType({"primary": frozenset(primary), "emergency": frozenset(emergency)}))


def local(day, hour):
    return time.mktime((2026, 10, day, hour, 0, 0, 0, 0, -1))


def clock(primary=(), emergency=(), resume=None):
    source = SimpleNamespace(snapshot=snap(primary, emergency), resume=resume)
    return b.BucketClock(source), source


# ------------------------------------------------------------------------------------- the clock, no database


def test_after_a_restart_the_bucket_in_use_goes_on_with_the_run_time_already_counted():
    c, _ = clock(primary=(3, 4, 5), resume={"current": 3, "run_seconds": 40000.0, "date": "2026-10-29"})
    c.tick(local(29, 20), 0.0)
    assert c.label() == 3 and c.progress == (3, 40000.0, "2026-10-29")


def test_a_resumed_bucket_that_can_take_nothing_new_is_not_used_again():
    c, _ = clock(primary=(4, 5), resume={"current": 3, "run_seconds": 40000.0, "date": "2026-10-29"})
    c.tick(local(29, 20), 0.0)
    assert c.label() == 4 and c.progress == (4, 0.0, "2026-10-29")


def test_the_next_bucket_needs_a_new_day_and_twelve_hours_of_run_time_on_disk():
    c, _ = clock(primary=(1, 2))
    c.tick(local(28, 10), 0.0)
    assert c.label() == 1
    c.tick(local(28, 23), 13 * HOUR)
    c.persisted = (1, 13 * HOUR)
    c.tick(local(28, 23) + 60, 13 * HOUR + 60)
    assert c.label() == 1, "twelve hours, but the same day"
    c.tick(local(29, 0) + 1800, 14.5 * HOUR)
    assert c.label() == 2 and c.progress == (2, 0.0, "2026-10-29"), "a new day and twelve hours on disk"


def test_twelve_hours_in_memory_are_not_enough_while_the_disk_holds_less():
    c, _ = clock(primary=(1, 2))
    c.tick(local(28, 10), 0.0)
    c.persisted = (1, 11 * HOUR)  # the last save before the twelfth hour
    c.tick(local(29, 1), 15 * HOUR)
    assert c.label() == 1, "a crash now would lose the evidence: the bucket is used longer"
    c.persisted = (1, 15 * HOUR)
    c.tick(local(29, 1) + 1, 15 * HOUR + 1)
    assert c.label() == 2


def test_run_time_is_the_process_running_never_the_wall_clock():
    c, _ = clock(primary=(1, 2))
    c.tick(local(28, 10), 0.0)
    c.tick(local(31, 10), 10.0)  # the wall clock jumped three days ahead, ten seconds passed
    assert c.progress[1] == 10.0
    c.persisted = (1, 10.0)
    c.tick(local(31, 10) + 1, 11.0)
    assert c.label() == 1, "a new date, but not twelve hours of running"


def test_without_a_prepared_next_bucket_the_current_one_is_used_longer():
    c, source = clock(primary=(1,))
    c.tick(local(28, 10), 0.0)
    c.persisted = (1, 13 * HOUR)
    c.tick(local(29, 1), 13 * HOUR)
    assert c.label() == 1
    source.snapshot = snap(primary=(1, 2))
    c.tick(local(29, 1) + 1, 13 * HOUR + 1)
    assert c.label() == 2, "taken as soon as it is ready, never waited for"


def test_with_no_bucket_ready_the_groups_carry_none_and_the_first_one_prepared_is_taken():
    c, source = clock()
    c.tick(local(28, 10), 0.0)
    assert c.label() is None
    source.snapshot = snap(emergency=(5,))
    c.tick(local(28, 10) + 1, 1.0)
    assert c.label() == 5, "a bucket only the emergency database has is better than none"


# ------------------------------------------------------------------------------------- the route, real databases


@pytest.fixture
def pair(archive_db_factory):
    cfg = config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")  # both have bucket 1
    router = ArchiveRouter(cfg, primary, emergency, disk_usage=Disks())
    router.initialize()
    router.bind_run("run1", cfg.config_id, 999.0)
    return router, cfg


def landed(store):
    with store.session() as conn:
        return [(x["group_id"], x["bucket"]) for x in conn.execute("SELECT group_id,bucket FROM commit_group ORDER BY 1")]


def test_a_group_without_a_bucket_is_refused_as_a_gap_and_nothing_is_written(pair):
    router, cfg = pair
    result = router.write_group(make_group(config_id=cfg.config_id, bucket=None))
    assert (result.status, result.reason) == ("REFUSED", "NO_BUCKET")
    assert landed(router.primary) == landed(router.emergency) == []


# a bucket the primary lacks in the primary route: test_retry_target (终版 §13.5, 修复轮09 审核第4条) - it waits there


def test_in_the_emergency_route_a_bucket_the_emergency_database_lacks_is_refused_as_a_gap(pair):
    router, cfg = pair
    router.disk_usage.used["D:/a"] = 0.96
    result = router.write_group(make_group(config_id=cfg.config_id, bucket=9))
    assert (result.status, result.reason) == ("REFUSED", "NO_BUCKET")
    assert router.pinned == {}, "certain: nothing was written anywhere"


def test_emergency_ends_only_once_the_primary_has_the_groups_bucket(pair):
    router, cfg = pair
    router.emergency.prepare_bucket(2)
    router.disk_usage.used["D:/a"] = 0.96
    assert router.write_group(make_group(order=1, config_id=cfg.config_id, bucket=1)).target == "emergency"
    router.disk_usage.used["D:/a"] = 0.80
    real, recorded = router.emergency.routing_mode, []

    def recording(value=None):
        if value:
            recorded.append(value)
        return real(value)

    router.emergency.routing_mode = recording
    assert router.write_group(make_group(order=2, config_id=cfg.config_id, bucket=2)).target == "emergency"
    assert router.mode == "EMERGENCY" and router.health["switchback"] == "NO_BUCKET"
    assert recorded == [], "the route is not switched back and forth: it is never left while the bucket is missing"
    router.primary.prepare_bucket(2)
    assert router.write_group(make_group(order=3, config_id=cfg.config_id, bucket=2)).target == "primary"


# ------------------------------------------------------------------------------- the saved run time, real databases


def test_the_run_time_is_saved_as_a_total_about_once_a_minute_and_confirmed_back(tmp_path, archive_db_factory):
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    now = [1000.0]
    m = b.Maintenance(small_config(), {"primary": primary.dsn, "emergency": emergency.dsn}, tmp_path / "n.json",
                      monotonic=lambda: now[0])  # fmt: skip
    c = b.BucketClock(m)
    m.clock = c
    m.cycle()
    c.tick(local(28, 10), 0.0)
    c.tick(local(28, 10) + 500, 500.0)
    m.cycle()
    saved = json.loads((tmp_path / "n.json").read_text(encoding="utf-8"))
    assert (saved["current"], saved["run_seconds"], saved["date"]) == (1, 500.0, "2026-10-28")
    assert c.persisted == (1, 500.0), "confirmed back only once it is on disk"
    c.tick(local(28, 10) + 530, 530.0)
    now[0] += 30
    m.cycle()
    assert json.loads((tmp_path / "n.json").read_text(encoding="utf-8"))["run_seconds"] == 500.0, "not every round"
    now[0] += 31
    m.cycle()
    assert json.loads((tmp_path / "n.json").read_text(encoding="utf-8"))["run_seconds"] == 530.0, "the total, never added up"


def test_the_run_time_is_not_saved_over_a_file_that_cannot_be_trusted(tmp_path, archive_db_factory):
    primary = archive_db_factory("primary")
    path = tmp_path / "n.json"
    path.write_bytes(b'{"max_no": 1, "cur')
    m = b.Maintenance(small_config(), {"primary": primary.dsn, "emergency": "postgresql://nobody@127.0.0.1:1/none"}, path)
    c = b.BucketClock(m)
    m.clock = c
    m.cycle()
    c.tick(local(28, 10), 0.0)
    c.tick(local(28, 10) + 500, 500.0)
    m.cycle()
    assert path.read_bytes() == b'{"max_no": 1, "cur', "the evidence of the numbers is not overwritten"
    assert c.persisted is None and m.snapshot.health["progress"].startswith("NOT_SAVED")
    assert m.resume is None, "a damaged file resumes nothing: the bucket starts its count again, it is only used longer"


# ------------------------------------------------------------------------------------- the start, real databases


@pytest.fixture
def number_file(tmp_path):
    return tmp_path / "bucket_number.json"


async def test_a_first_start_of_empty_databases_waits_briefly_and_its_first_groups_have_a_bucket(archive_db_factory, number_file):
    cfg = small_config()
    primary, emergency = archive_db_factory("primary", prepare=False), archive_db_factory("emergency", prepare=False)
    runtime = Runtime(cfg, ArchiveRouter(cfg, primary, emergency, disk_usage=Disks()), number_file=number_file)
    started = time.monotonic()
    await runtime.start()
    try:
        assert time.monotonic() - started < 5.5
        deadline = time.monotonic() + 10
        while not landed(primary) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert landed(primary) and all(bucket == 1 for _, bucket in landed(primary)), "the run's start is in bucket 1"
        assert runtime.queue.stats["refused"] == 0, "no gap at a first start"
    finally:
        await runtime.stop()
    assert json.loads(number_file.read_text(encoding="utf-8"))["current"] == 1, "saved at the normal stop"


async def test_the_start_waits_for_buckets_five_seconds_at_most_and_goes_on_preparing(archive_db_factory, number_file, monkeypatch):
    cfg = small_config()
    primary, emergency = archive_db_factory("primary", prepare=False), archive_db_factory("emergency", prepare=False)
    real, rounds = b.Maintenance.cycle, []

    def slow(self):
        rounds.append(1)
        if len(rounds) == 1:
            time.sleep(8)  # the databases are slow to answer at this start
        real(self)

    monkeypatch.setattr(b.Maintenance, "cycle", slow)
    runtime = Runtime(cfg, ArchiveRouter(cfg, primary, emergency, disk_usage=Disks()), number_file=number_file)
    started = time.monotonic()
    await runtime.start()
    try:
        assert 4.5 < time.monotonic() - started < 6.5, "five seconds for the whole preparation, then acquisition starts"
        assert runtime.running
        deadline = time.monotonic() + 15
        while not runtime.maintenance.usable.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        assert runtime.maintenance.usable.is_set(), "the background task went on and prepared them"
        assert [t.name for t in threading.enumerate()].count("bucket maintenance") == 1, "the same task, not a second one"
    finally:
        await runtime.stop()
