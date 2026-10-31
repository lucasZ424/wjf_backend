"""What the self-check page says about the rolling deletion, and the high-water alarm (修复轮09 09h, 终版 §13.6 自检与报警).

Why it matters: nobody watches the disk on site.  So the page must say per database what is kept, when the last deletion was
and how often it really happens, and when the next one is expected - or plainly that it cannot be foreseen and why.  The
alarm comes only when the normal disk stays above the start line for ten minutes, never during normal retention, and it
says why the deletion could not bring it down.
"""

import time

import pytest
from test_archive_router import GIB
from test_archive_write import make_group
from test_rolling_delete import build, rig

from htsg import buckets as b

DAY = 86400.0


@pytest.fixture
def pair(archive_db_factory):
    return archive_db_factory("primary", prepare=False), archive_db_factory("emergency", prepare=False)


@pytest.fixture
def clock():
    return [1000.0]


def test_the_alarm_comes_only_after_ten_minutes_above_the_start_line_and_says_why(tmp_path, pair, clock):
    m, cfg = rig(tmp_path, pair, keep=0, floor=14, monotonic=lambda: clock[0])
    build(pair[0], cfg, 10)  # too few buckets: nothing may be deleted
    m.cycle()
    assert m.snapshot.health["retention"]["alarm"]["active"] is False
    clock[0] += 599
    m.cycle()
    assert m.snapshot.health["retention"]["alarm"]["active"] is False, "nine minutes and 59 seconds: not yet"
    clock[0] += 2
    m.cycle()
    alarm = m.snapshot.health["retention"]["alarm"]
    assert alarm["active"] is True and alarm["reasons"] == ["NO_ELIGIBLE_RANGE"]
    m.disk_usage.full = 0.85  # between the lines: below the start line again
    m.cycle()
    assert m.snapshot.health["retention"]["alarm"]["active"] is False


@pytest.mark.parametrize(
    ("deletion", "reason"),
    [("NO_RANGE:NO_BASELINE", "BOUNDARY_WITHOUT_BASELINE"), ("NO_RANGE:FLOOR", "NO_ELIGIBLE_RANGE"),
     ("POSTPONED:QueryCanceled", "POSTPONED_BY_LONG_TRANSACTION"), ("POSTPONED:OperationalError", "DELETION_FAILING"),
     ("SWITCHED_OFF", "DELETION_SWITCHED_OFF")],
)  # fmt: skip
def test_every_reason_the_deletion_gives_is_named_in_the_alarm(deletion, reason):
    assert b.alarm_reasons({"deletion": deletion}, usable=True, other_files=False) == [reason]


def test_the_alarm_names_a_missing_bucket_and_files_other_than_the_database():
    assert b.alarm_reasons({"deletion": "IDLE"}, usable=False, other_files=True) == ["NO_WRITABLE_BUCKET", "OTHER_FILES_GROWING"]


def test_the_page_says_what_each_database_keeps_and_when_the_last_deletion_was(tmp_path, pair, clock):
    m, cfg = rig(tmp_path, pair, keep=10)
    build(pair[0], cfg, 20)
    m.cycle()
    report = m.snapshot.health["retention"]
    kept = report["databases"]["primary"]
    assert kept["kept_buckets"] == 10 and kept["boundary"] == 1011.0, "the oldest bucket kept begins with its baseline"
    assert report["last_deletion"] is not None and abs(report["last_deletion"] - time.time()) < 60
    assert report["databases"]["emergency"]["kept_buckets"] == 0


def test_the_next_deletion_cannot_be_foreseen_from_less_than_seven_days(tmp_path, pair):
    primary, _ = pair
    m, cfg = rig(tmp_path, pair, keep=0, full=0.5)
    now = time.time()
    for n in range(1, 4):  # two days of history, the newest bucket current
        primary.prepare_bucket(n)
        group = make_group(order=n, config_id=cfg.config_id, bucket=n, built_wall=now - (3 - n) * DAY)
        assert primary.write_group(group).durable
    m.cycle()
    forecast = m.snapshot.health["retention"]["forecast"]
    assert forecast == {"next_deletion": None, "reason": "INSUFFICIENT_DATA"}


def test_the_next_deletion_is_foreseen_from_the_buckets_of_the_last_seven_days(tmp_path, pair):
    primary, _ = pair
    m, cfg = rig(tmp_path, pair, keep=0, full=0.5)
    now = time.time()
    for n in range(1, 11):
        primary.prepare_bucket(n)
        group = make_group(order=n, n_obs=50, config_id=cfg.config_id, bucket=n, built_wall=now - (10 - n) * DAY)
        assert primary.write_group(group).durable
    m.cycle()
    forecast = m.snapshot.health["retention"]["forecast"]
    assert forecast["reason"] is None and forecast["next_deletion"] > now, forecast
    per_day = forecast["bytes_per_day"]
    room = (cfg.storage.cleanup_start - 0.5) * 1000 * GIB
    assert abs(forecast["next_deletion"] - (now + room / per_day * DAY)) < 2 * DAY, "room to the start line / growth per day"


def test_the_page_gets_both_volumes_and_the_lines_they_are_judged_by_and_an_unreadable_one_ends_nothing(tmp_path, pair):
    """修复轮12 (终版 第15节 自检页“磁盘占用”): the self-check page had the database sizes but no volume - the one figure the
    deletion and the emergency route are decided by.  A volume that cannot be read is said to be so; the round goes on."""
    m, cfg = rig(tmp_path, pair, keep=10, full=0.5)
    m.disk_usage.below, m.disk_usage.used["D:/b"] = 0.5, 0.25  # no bucket left: the "below" reading
    m.cycle()
    disk = m.snapshot.health["retention"]["disk"]
    assert disk["primary"]["used_fraction"] == pytest.approx(0.5) and disk["primary"]["total"] == 1000 * GIB
    assert disk["emergency"]["free"] == int(1000 * GIB * 0.75)
    s = cfg.storage
    assert (disk["cleanup_start"], disk["cleanup_stop"], disk["emergency_start"], disk["emergency_reserve_bytes"]) == (
        s.cleanup_start, s.cleanup_stop, s.emergency_start, s.emergency_reserve_bytes)  # fmt: skip
    reading = m.disk_usage

    def gone(path):
        if path == "D:/b":
            raise OSError("the emergency volume is gone")
        return reading(path)

    m.disk_usage = gone
    m.cycle()
    disk = m.snapshot.health["retention"]["disk"]
    assert disk["emergency"] is None and disk["primary"]["used_fraction"] == pytest.approx(0.5)
