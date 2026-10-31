"""Generation 6 on (7 since 修复轮14): the record tables are partitioned by bucket, and a write is only taken by a bucket that may take it
(修复轮09 09b, 终版 §13.6 桶 / 写入资格, plan D1, D5, D6).

Why it matters: the rolling deletion takes whole buckets away.  That is only safe if every group lies in exactly one bucket,
if a bucket being deleted can no longer receive a group (the share lock of the write and the exclusive lock of the marking
exclude each other), and if a group the queue still holds for a deleted bucket becomes a gap instead of being slipped into
the current one.  Preparing a bucket must never wait for, or hold up, the writes going on.
"""

import psycopg
import pytest
from owner_support import small_config
from test_archive_router import Disks, config
from test_archive_write import make_group

from htsg.archive import SCHEMA_GENERATION, SchemaError, WriteOutcome
from htsg.archive_router import ArchiveRouter

CONFIG = small_config()


@pytest.fixture
def store(archive_db_factory):
    s = archive_db_factory()  # initialised, bucket 1 prepared (conftest)
    s.bind_run("run1", CONFIG.config_id, 999.0)
    return s


def states(store):
    with store.session() as conn:
        return {x["no"]: x["state"] for x in conn.execute("SELECT no,state FROM bucket ORDER BY no").fetchall()}


def partition_of(store, table, bucket):
    with store.session() as conn:
        return {x["p"] for x in conn.execute(f"SELECT tableoid::regclass::text AS p FROM {table} WHERE bucket=%s", (bucket,))}


def total(store, table):
    with store.session() as conn:
        return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_the_generation_is_7_and_a_generation_6_database_is_refused_unchanged(archive_db_factory):
    assert SCHEMA_GENERATION == 7
    old = archive_db_factory(prepare=False)
    with old.session() as conn:
        conn.execute("CREATE TABLE htsg_meta (key text PRIMARY KEY, value text NOT NULL)")
        conn.execute("INSERT INTO htsg_meta VALUES ('schema_generation','6')")
    with pytest.raises(SchemaError, match="generation 6"):
        old.initialize(CONFIG)
    with old.session() as conn:
        assert conn.execute("SELECT to_regclass('public.bucket') AS t").fetchone()["t"] is None, "nothing was added"


def test_every_record_of_a_group_lies_in_the_partitions_of_its_bucket(store):
    store.prepare_bucket(2)
    assert store.write_group(make_group(order=1, bucket=1)).status == "COMMITTED"
    assert store.write_group(make_group(order=2, bucket=2)).status == "COMMITTED"
    for table in ("commit_group", "sample"):  # a group without a baseline puts nothing into state_event
        assert partition_of(store, table, 1) == {f"{table}_b1"} and partition_of(store, table, 2) == {f"{table}_b2"}
    assert total(store, "fault_record") == 2, "fault records are kept for good, outside the buckets"


def test_a_bucket_this_database_does_not_have_takes_nothing_and_says_so(store):
    outcome = store.write_group(make_group(bucket=7))
    assert outcome == WriteOutcome("FAILED", "NO_BUCKET"), "certain: nothing was written"
    assert total(store, "commit_group") == 0 and total(store, "sample") == 0


@pytest.mark.parametrize("state", ["DELETING", "DELETED"])
def test_a_group_for_a_bucket_being_or_already_deleted_is_not_written_and_not_moved_to_another(store, state):
    with store.session() as conn:
        conn.execute("UPDATE bucket SET state=%s WHERE no=1", (state,))
    store.prepare_bucket(2)
    outcome = store.write_group(make_group(bucket=1))
    assert outcome == WriteOutcome("RETIRED", "BUCKET_RETIRED") and not outcome.durable
    assert total(store, "commit_group") == 0 and total(store, "sample") == 0, "not written into bucket 2 either"


def test_the_first_group_of_a_bucket_makes_it_current_and_seals_the_older_one_which_still_takes_its_own_late_groups(store):
    store.prepare_bucket(2)
    assert states(store) == {1: "READY", 2: "READY"}
    store.write_group(make_group(order=1, bucket=1))
    assert states(store) == {1: "CURRENT", 2: "READY"}
    store.write_group(make_group(order=2, bucket=2))
    assert states(store) == {1: "SEALED", 2: "CURRENT"}
    late = store.write_group(make_group(order=3, bucket=1))  # given to bucket 1 before it was sealed, still in the queue
    assert late.status == "COMMITTED" and states(store) == {1: "SEALED", 2: "CURRENT"}, "nothing un-seals, nothing moves"


def test_a_repeated_group_is_already_there_only_in_its_own_bucket(store):
    store.prepare_bucket(2)
    group = make_group(bucket=1)
    assert store.write_group(group).status == "COMMITTED"
    assert store.write_group(group) == WriteOutcome("ALREADY")
    assert store.group_committed(group.group_id, 1) is True
    assert store.group_committed(group.group_id, 2) is False, "the question is asked of the group's own bucket"


def test_the_write_and_the_marking_for_deletion_exclude_each_other(store):
    store.write_group(make_group(order=1, bucket=1))
    with psycopg.connect(store.dsn) as other:
        other.execute("SELECT state FROM bucket WHERE no=1 FOR UPDATE")  # what the deletion holds while it marks
        outcome = store.write_group(make_group(order=2, bucket=1))
        other.rollback()
    assert outcome == WriteOutcome("FAILED", "LockNotAvailable"), "certain, and the queue tries it again"
    assert store.write_group(make_group(order=2, bucket=1)).status == "COMMITTED"


def test_preparing_a_bucket_neither_waits_for_nor_holds_up_a_write_in_progress(store):
    store.write_group(make_group(order=1, bucket=1))
    with psycopg.connect(store.dsn) as writing:  # a write of bucket 1 not yet committed: it holds its table locks
        writing.execute("INSERT INTO write_probe(id,touched_at) VALUES(1,now())")
        writing.execute("UPDATE sample SET value=value WHERE bucket=1")
        store.prepare_bucket(2)  # the store's own limits: 1 s lock wait - waiting would fail here
        writing.commit()
    assert states(store) == {1: "CURRENT", 2: "READY"}
    assert store.prepare_bucket(2) is False, "a second preparation of the same bucket changes nothing"


def test_the_router_turns_a_retired_bucket_into_a_gap_reason(archive_db_factory):
    cfg = config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    router = ArchiveRouter(cfg, primary, emergency, disk_usage=Disks())
    router.initialize()
    router.bind_run("run1", cfg.config_id, 999.0)
    with primary.session() as conn:
        conn.execute("UPDATE bucket SET state='DELETED' WHERE no=1")
    result = router.write_group(make_group(config_id=cfg.config_id, bucket=1))
    assert (result.status, result.reason) == ("REFUSED", "BUCKET_RETIRED")
    assert router.pinned == {}, "nothing to verify later: it is certain the group is not stored"
