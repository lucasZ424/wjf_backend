"""The maintenance thread: bucket numbers and prepared buckets (修复轮09 09c, 终版 §13.6 桶号 / 准备与换桶, plan D2-D4, D7, D8).

Why it matters: a bucket number used twice would put two different stretches of history under one name, and the rolling
deletion would take both away together.  So a number comes from the highest one any database or the number file has used,
the file is on disk before the number is used, and when the file cannot be trusted and the databases cannot both be read the
thread allocates nothing - the buckets already prepared go on being used, acquisition never waits.  A first start of empty
databases is told apart from a deployment that lost its file.  The thread does its database work on its own connections
(60 s per statement, 1 s lock wait), hands back an immutable snapshot, and an error in one round never ends it.
"""

import json

import psycopg
import pytest
from owner_support import small_config

from htsg import buckets as b

CONFIG = small_config()


class Offline:
    """A database that cannot be reached right now."""

    dsn = "postgresql://nobody@127.0.0.1:1/none"


@pytest.fixture
def pair(archive_db_factory):
    primary, emergency = archive_db_factory("primary", prepare=False), archive_db_factory("emergency", prepare=False)
    primary.initialize(CONFIG)
    emergency.initialize(CONFIG)
    return primary, emergency


def maintenance(tmp_path, primary, emergency, **kw):
    dsns = {"primary": primary.dsn, "emergency": emergency.dsn}
    return b.Maintenance(CONFIG, dsns, tmp_path / "bucket_number.json", **kw)


def catalog(store):
    with psycopg.connect(store.dsn) as conn:
        return dict(conn.execute("SELECT no,state FROM bucket ORDER BY no").fetchall())


def numbers(tmp_path):
    return json.loads((tmp_path / "bucket_number.json").read_text(encoding="utf-8"))["max_no"]


def test_a_first_start_of_empty_databases_prepares_seven_buckets_in_both_and_says_one_is_usable(tmp_path, pair):
    primary, emergency = pair
    m = maintenance(tmp_path, primary, emergency)
    assert not m.usable.is_set()
    m.cycle()
    assert catalog(primary) == catalog(emergency) == {n: "READY" for n in range(1, 8)}
    assert numbers(tmp_path) == 7, "on disk before the numbers were used"
    assert m.snapshot.usable == {"primary": frozenset(range(1, 8)), "emergency": frozenset(range(1, 8))}
    assert m.usable.is_set() and m.snapshot.health["numbering"] == "FIRST_START"
    m.cycle()
    assert catalog(primary) == {n: "READY" for n in range(1, 8)}, "seven ahead is enough: nothing more is made"


def test_a_number_is_used_only_once_the_file_holding_it_is_on_disk(tmp_path, pair, monkeypatch):
    primary, emergency = pair
    m = maintenance(tmp_path, primary, emergency)

    def disk_full(*_):
        raise OSError("no space left")

    monkeypatch.setattr(b.os, "replace", disk_full)
    m.cycle()
    assert catalog(primary) == {} and catalog(emergency) == {}, "no bucket under a number the file does not hold"
    assert m.snapshot.health["numbering"].startswith("FILE_NOT_WRITTEN") and not m.usable.is_set()


def test_with_the_emergency_database_away_numbers_go_on_and_its_missing_buckets_are_made_when_it_is_back(tmp_path, pair):
    primary, emergency = pair
    m = maintenance(tmp_path, primary, emergency)
    m.cycle()
    away = maintenance(tmp_path, primary, Offline())
    with psycopg.connect(primary.dsn, autocommit=True) as conn:  # the first three were used up meanwhile
        conn.execute("UPDATE bucket SET state='SEALED' WHERE no<=3")
    away.cycle()
    assert set(catalog(primary)) == set(range(1, 11)), "a reliable file: new numbers while one database is away"
    assert away.snapshot.usable["emergency"] == frozenset() and away.snapshot.health["emergency"] == "UNAVAILABLE"
    back = maintenance(tmp_path, primary, emergency)
    back.cycle()
    assert set(catalog(emergency)) == set(range(1, 11)), "the buckets made while it was away are made there too"
    assert numbers(tmp_path) == 10


def test_a_lost_file_with_both_databases_readable_goes_on_from_their_highest_number(tmp_path, pair):
    primary, emergency = pair
    maintenance(tmp_path, primary, emergency).cycle()
    (tmp_path / "bucket_number.json").unlink()
    with psycopg.connect(primary.dsn, autocommit=True) as conn:
        conn.execute("UPDATE bucket SET state='SEALED' WHERE no<=2")
    m = maintenance(tmp_path, primary, emergency)
    m.cycle()
    assert set(catalog(primary)) == set(range(1, 10)) and numbers(tmp_path) == 9
    assert m.snapshot.health["numbering"] == "FILE_REBUILT", "not taken for a first start: the databases have buckets"


@pytest.mark.parametrize("damage", ["missing", "torn", "behind"])
def test_an_unreliable_file_with_a_database_away_allocates_nothing_and_the_prepared_buckets_go_on(tmp_path, pair, damage):
    primary, emergency = pair
    for n in range(1, 21):  # the emergency database used up to 20; the primary only knows up to 10
        emergency.prepare_bucket(n)
        if n <= 10:
            primary.prepare_bucket(n)
    path = tmp_path / "bucket_number.json"
    if damage == "torn":
        path.write_bytes(b'{"max_no": 2')
    elif damage == "behind":
        path.write_text(json.dumps({"max_no": 5}), encoding="utf-8")
    with psycopg.connect(primary.dsn, autocommit=True) as conn:
        conn.execute("UPDATE bucket SET state='SEALED' WHERE no<=6")  # only 4 ready: a new one would be wanted
    m = maintenance(tmp_path, primary, Offline())
    m.cycle()
    assert set(catalog(primary)) == set(range(1, 11)), "no 11: it may already be used in the database that is away"
    assert m.snapshot.health["numbering"].startswith("PAUSED")
    assert m.snapshot.usable["primary"] == frozenset(range(7, 11)), "the buckets already there are still handed out"


def test_the_threads_connections_wait_60_s_per_statement_and_1_s_for_a_lock(tmp_path, pair):
    primary, emergency = pair
    m = maintenance(tmp_path, primary, emergency)
    with m.stores["primary"].session() as conn:
        limits = {k: conn.execute(f"SHOW {k}").fetchone()[k] for k in ("statement_timeout", "lock_timeout")}
    assert limits == {"statement_timeout": "1min", "lock_timeout": "1s"}
    with primary.session() as conn:
        assert conn.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "3s", "the writer's stays 3 s"


def test_an_error_in_one_round_does_not_end_the_thread(tmp_path, pair):
    primary, emergency = pair
    m = maintenance(tmp_path, primary, emergency, interval=0.05)
    real, calls = m.cycle, []

    def once_broken():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("something unforeseen")
        real()

    m.cycle = once_broken
    m.start()
    try:
        assert m.usable.wait(20), "the next round prepared the buckets"
        assert m.thread.is_alive() and m.snapshot.health["error"].startswith("RuntimeError")
    finally:
        m.stop(5)
    assert not m.thread.is_alive()


def test_the_snapshot_is_handed_over_whole_and_cannot_be_changed_by_its_reader(tmp_path, pair):
    primary, emergency = pair
    m = maintenance(tmp_path, primary, emergency)
    m.cycle()
    snap = m.snapshot
    with pytest.raises((AttributeError, TypeError)):
        snap.usable["primary"] = frozenset()
    with psycopg.connect(primary.dsn, autocommit=True) as conn:
        conn.execute("UPDATE bucket SET state='SEALED' WHERE no<=2")
    m.cycle()
    assert 1 not in m.snapshot.usable["primary"], "the new snapshot says what the database says now"
    assert snap.usable["primary"] == frozenset(range(1, 8)), "the one already handed out was replaced, never changed"


def test_a_bucket_that_cannot_be_prepared_where_it_counts_ends_the_round_instead_of_using_up_numbers(tmp_path, pair):
    primary, emergency = pair
    m = maintenance(tmp_path, primary, emergency)

    def refused(no):
        raise psycopg.errors.InsufficientPrivilege("no")

    m.stores["primary"].prepare_bucket = refused
    m.cycle()
    assert numbers(tmp_path) == 1, "one number tried, not seven thrown away every round"
    assert m.snapshot.health["primary"] == "PREPARE_FAILED:InsufficientPrivilege"
