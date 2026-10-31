"""The rolling deletion of the normal database (修复轮09 09f, 终版 §13.4, §13.6 可删范围 / 删除步骤 / 维护连接 / 应急库).

Why it matters: the normal database must never fill the disk, and what is taken away must leave a history that still reads
right.  So deletion starts at the start line and goes whole bucket by whole bucket from the oldest until below the stop line;
it only takes a continuous range ending just before a bucket that begins with a complete baseline; it never takes a bucket a
group is still on its way to, nor anything inside the floor of the newest buckets; a range once marked is taken away to the
end, after a crash as well, and the switch only stops NEW deletions.  A long transaction only postpones it.  The emergency
database is never thinned out.
"""

from dataclasses import replace

import psycopg
import pytest
from test_archive_router import Disks, config
from test_archive_write import make_group
from test_retention_baseline import baseline_group

from htsg import buckets as b
from htsg.archive import PARTITIONED


def build(store, cfg, count, *, without_baseline=(), used=None, first_order=1):
    """Buckets 1..count, one group in each that is used here (the newest is current, the others sealed), and a baseline in
    every used one except `without_baseline`."""
    used = set(range(1, count + 1)) if used is None else set(used)
    for n in range(1, count + 1):
        store.prepare_bucket(n)
    order = first_order
    for n in sorted(used):
        assert store.write_group(make_group(order=order, config_id=cfg.config_id, bucket=n)).durable
        if n not in without_baseline:
            assert store.write_group(replace_bucket(baseline_group(cfg, order + 1, 1000.0 + n), n)).durable
        order += 2
    if count not in used:  # the newest number is current here even when its groups went elsewhere
        assert store.write_group(make_group(order=order, config_id=cfg.config_id, bucket=count)).durable


def replace_bucket(group, n):
    return replace(group, bucket=n)


def states(store):
    with psycopg.connect(store.dsn) as conn:
        return dict(conn.execute("SELECT no,state FROM bucket ORDER BY no").fetchall())


def deleted(store):
    return {n for n, s in states(store).items() if s == "DELETED"}


def partitions(store, n):
    with psycopg.connect(store.dsn) as conn:
        return {x[0] for x in conn.execute("SELECT relname FROM pg_class WHERE relname = ANY(%s)", ([f"{t}_b{n}" for t in PARTITIONED],))}


def logged(store):
    with psycopg.connect(store.dsn) as conn:
        return [x[0] for x in conn.execute("SELECT target FROM management_log WHERE action='ROLLING_DELETE' ORDER BY time")]


class Usage(Disks):
    """The normal disk is full while more than `keep` used buckets are left in the normal database."""

    def __init__(self, store, keep, full=0.95, below=0.79):
        super().__init__()
        self.store, self.keep, self.full, self.below = store, keep, full, below

    def __call__(self, path):
        live = sum(1 for s in states(self.store).values() if s in ("CURRENT", "SEALED"))
        self.used["D:/a"] = self.full if live > self.keep else self.below
        return super().__call__(path)


@pytest.fixture
def pair(archive_db_factory):
    return archive_db_factory("primary", prepare=False), archive_db_factory("emergency", prepare=False)


def rig(tmp_path, pair, *, keep, floor=3, enabled=True, full=0.95, **kw):
    primary, emergency = pair
    cfg = config(cleanup_enabled=enabled, retention_min_buckets=floor)
    for store in pair:
        store.initialize(cfg)
    m = b.Maintenance(cfg, {"primary": primary.dsn, "emergency": emergency.dsn}, tmp_path / "n.json",
                      disk_usage=Usage(primary, keep, full=full), **kw)  # fmt: skip
    return m, cfg


def test_below_the_start_line_nothing_is_deleted(tmp_path, pair):
    m, cfg = rig(tmp_path, pair, keep=0, full=0.89)
    build(pair[0], cfg, 20)
    m.cycle()
    assert deleted(pair[0]) == set()


def test_at_the_start_line_whole_buckets_go_from_the_oldest_until_below_the_stop_line(tmp_path, pair):
    m, cfg = rig(tmp_path, pair, keep=10)
    build(pair[0], cfg, 20)
    m.cycle()
    assert deleted(pair[0]) == set(range(1, 11)), "oldest first, and no further once below the stop line"
    assert all(partitions(pair[0], n) == set() for n in range(1, 11)) and partitions(pair[0], 11)
    assert len(logged(pair[0])) == 10, "one entry in the management log for each range taken away"


def test_a_bucket_without_its_baseline_goes_with_the_older_ones_up_to_the_next_that_has_one(tmp_path, pair):
    m, cfg = rig(tmp_path, pair, keep=17)
    build(pair[0], cfg, 20, without_baseline={2, 3})
    m.cycle()
    assert deleted(pair[0]) == {1, 2, 3}, "bucket 4 begins with the state in force: the range ends just before it"
    assert logged(pair[0]) == ["buckets 1-3"], "one range, taken together"


def test_nothing_starts_while_a_bucket_of_the_range_still_has_a_group_on_its_way(tmp_path, pair):
    m, cfg = rig(tmp_path, pair, keep=0)
    build(pair[0], cfg, 20, without_baseline={2})
    m.unsettled = frozenset({2})
    m.cycle()
    assert deleted(pair[0]) == set(), "not bucket 1 alone either: the range is 1-2, whole or not at all"
    assert m.snapshot.health["deletion"] == "NO_RANGE:UNSETTLED_GROUPS"


def test_the_floor_counts_the_used_sealed_buckets_once_over_both_databases(tmp_path, pair):
    primary, emergency = pair
    m, cfg = rig(tmp_path, pair, keep=0, floor=14)
    build(primary, cfg, 16, used=set(range(1, 11)) | {16})  # 11-15 went to the emergency database
    for n in range(1, 17):
        emergency.prepare_bucket(n)
    for n in range(11, 17):  # 16 too: until a newer bucket's group arrives there, 15 is still current in it (plan D1)
        assert emergency.write_group(make_group(order=100 + n, config_id=cfg.config_id, bucket=n)).durable
    m.cycle()
    assert deleted(primary) == {1}, "2-15 are fourteen newer used buckets for 1; only thirteen for 2"
    assert m.snapshot.health["deletion"] == "NO_RANGE:FLOOR"
    assert deleted(emergency) == set(), "the emergency database is never thinned out"


def test_with_the_switch_off_no_new_deletion_starts_but_a_range_already_marked_is_taken_away_to_the_end(tmp_path, pair):
    m, cfg = rig(tmp_path, pair, keep=0, enabled=False)
    build(pair[0], cfg, 20)
    with psycopg.connect(pair[0].dsn, autocommit=True) as conn:
        conn.execute("UPDATE bucket SET state='DELETING' WHERE no IN (1,2)")
    m.cycle()
    assert deleted(pair[0]) == {1, 2}
    assert partitions(pair[0], 1) == partitions(pair[0], 2) == set()


def detach_pending(store, n):
    """Leave bucket n's sample partition half detached, the way a crash or a timeout inside DETACH CONCURRENTLY does."""
    reader = psycopg.connect(store.dsn)
    reader.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    reader.execute("SELECT count(*) FROM sample")
    with psycopg.connect(store.dsn, autocommit=True) as conn:
        conn.execute("SET statement_timeout='1s'")
        with pytest.raises(psycopg.errors.QueryCanceled):
            conn.execute(f"ALTER TABLE sample DETACH PARTITION sample_b{n} CONCURRENTLY")
        assert conn.execute("SELECT count(*) FROM pg_inherits WHERE inhdetachpending").fetchone()[0] == 1
    reader.close()


@pytest.mark.parametrize("stage", ["marked", "half", "pending"])
def test_a_deletion_interrupted_at_any_step_is_finished_by_the_next_round(tmp_path, pair, stage):
    m, cfg = rig(tmp_path, pair, keep=0, enabled=False)
    build(pair[0], cfg, 20)
    with psycopg.connect(pair[0].dsn, autocommit=True) as conn:
        conn.execute("UPDATE bucket SET state='DELETING' WHERE no=1")
        if stage == "half":
            for table in ("commit_group", "sample"):
                conn.execute(f"ALTER TABLE {table} DETACH PARTITION {table}_b1")
            conn.execute("DROP TABLE commit_group_b1")
    if stage == "pending":
        detach_pending(pair[0], 1)
    m.cycle()
    assert deleted(pair[0]) == {1} and partitions(pair[0], 1) == set()
    with psycopg.connect(pair[0].dsn) as conn:
        assert conn.execute("SELECT count(*) FROM pg_inherits WHERE inhdetachpending").fetchone()[0] == 0


def test_a_long_transaction_postpones_the_deletion_and_the_next_round_finishes_it(tmp_path, pair):
    m, cfg = rig(tmp_path, pair, keep=19, statement_seconds=2)
    build(pair[0], cfg, 20)
    reader = psycopg.connect(pair[0].dsn)
    reader.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    reader.execute("SELECT count(*) FROM sample")  # a long query of the history page
    m.cycle()
    assert states(pair[0])[1] == "DELETING" and m.snapshot.health["deletion"].startswith("POSTPONED")
    reader.close()
    m.cycle()
    assert deleted(pair[0]) == {1} and partitions(pair[0], 1) == set()
