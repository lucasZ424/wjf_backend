"""History after the rolling deletion (修复轮09 09g, 终版 §10.1, §13.5, §13.6 保留边界与查询, §14 保留边界).

Why it matters: a window that reaches into deleted data must say so per database instead of looking empty, a range being
deleted is "no longer queryable" rather than "no data", and inside what is kept only the stretch of the oldest bucket before
its baseline is "possibly incomplete" - deleted and possibly incomplete are two different statements and are never merged.
A value held across the boundary comes from the baseline and counts only from it.  A database is read in one repeatable-read,
read-only transaction, so a deletion going on cannot hand a query half a bucket.
"""

from dataclasses import replace

import psycopg
import pytest
from owner_support import small_config
from test_archive_write import make_group
from test_retention_baseline import baseline_group

from htsg import history as h
from htsg import records as r

CFG = small_config()
CHANNELS = {"ti": "ua"}


def put(store, order, bucket, value=None):
    group = make_group(order=order, n_obs=1, config_id=CFG.config_id, bucket=bucket, built_wall=1000.0 + order)
    if value is not None:
        group = replace(group, observations=(replace(group.observations[0], value=value),))
    assert store.write_group(group).durable
    return group


def baseline(store, order, bucket, wall, restated):
    """Bucket `bucket` begins with ti = restated.value at `wall`, the way the state owner restates it."""
    obs = restated.observations[0]
    copy = replace(obs, id=r.baseline_id("run1", order, "o", "ti"), origin=r.ORIGIN_BASELINE, orig_id=obs.id,
                   batch_seq=order - 1, asserted_wall=wall, asserted_mono=50.0, item_seq=2**31 - 1)  # fmt: skip
    group = replace(baseline_group(CFG, order, wall), observations=(copy,), bucket=bucket, built_wall=wall)
    assert store.write_group(group).durable


@pytest.fixture
def store(archive_db_factory):
    s = archive_db_factory()
    s.bind_run("run1", CFG.config_id, 999.0)
    for n in (2, 3):
        s.prepare_bucket(n)
    first = put(s, 1, 1, value=481.0)  # ti from 1001 in bucket 1 ...
    put(s, 2, 2, value=481.0)  # ... bucket 2 at 1002 (same value: no new history)
    baseline(s, 3, 2, 1002.5, first)
    put(s, 4, 3, value=490.0)  # a change at 1004 in bucket 3
    return s


def retire(store, n, state):
    with store.session() as conn:
        conn.execute("UPDATE bucket SET state='SEALED' WHERE no=%s", (n,))
    assert store.mark_deleting([n], n + 1)
    if state == "DELETED":
        assert store.retire_bucket(n)


def shown(result):  # a row no longer says how it came to be (修复轮14): the baseline is the one at its boundary time
    return [(x["value"], x["time"]) for s in result["channels"]["ti"]["segments"] for x in s["rows"]]


def test_a_range_being_deleted_is_marked_no_longer_queryable_and_not_shown(store):
    retire(store, 1, "DELETING")
    result = h.query([("primary", store)], CHANNELS, 1000.0, 1010.0)
    assert 1001.0 not in [t for _, t in shown(result)], "rows of a bucket being deleted are not shown"
    (retired,) = result["retention"]["primary"]["retired"]
    assert (retired["state"], retired["from"], retired["to"]) == ("DELETING", 1001.0, 1001.0)


def test_before_the_boundary_is_deleted_and_inside_the_oldest_kept_bucket_only_possibly_incomplete(store):
    retire(store, 1, "DELETED")
    result = h.query([("primary", store)], CHANNELS, 1000.0, 1010.0)
    kept = result["retention"]["primary"]
    assert kept["deleted_before"] == 1002.5, "the boundary is the earliest baseline of the oldest bucket kept"
    assert [(x["state"], x["from"], x["to"]) for x in kept["retired"]] == [("DELETED", 1001.0, 1001.0)]
    assert [(x["from"], x["to"]) for x in kept["possibly_incomplete"]] == [(1002.0, 1002.5)], "bucket start to its baseline"
    assert shown(result)[0] == (481.0, 1002.0)
    assert (481.0, 1002.5) in shown(result), "the baseline counts from its boundary, never before it"


def test_a_window_held_across_the_boundary_takes_its_value_from_the_baseline(store):
    retire(store, 1, "DELETED")
    with store.session() as conn:
        conn.execute("DELETE FROM sample WHERE bucket=2 AND item<>32767")  # only the baseline (last in order) is left
    result = h.query([("primary", store)], CHANNELS, 1003.0, 1003.5)
    (held,) = [x for s in result["channels"]["ti"]["segments"] for x in s["rows"] if x["held"]]
    assert (held["value"], held["time"]) == (481.0, 1002.5), "the baseline's value, from the baseline's time"


def test_a_database_that_never_deleted_anything_says_nothing_about_deletion(store):
    result = h.query([("primary", store)], CHANNELS, 1000.0, 1010.0)
    assert result["retention"]["primary"] == {"retired": [], "possibly_incomplete": [], "deleted_before": None}


def test_each_database_is_read_in_one_repeatable_read_read_only_transaction(store, monkeypatch):
    seen = []
    real = h._rows

    def spying(conn, sql, params):
        seen.append(tuple(conn.execute(f"SHOW {k}").fetchone()[k] for k in ("transaction_isolation", "transaction_read_only")))
        return real(conn, sql, params)

    monkeypatch.setattr(h, "_rows", spying)
    h.query([("primary", store)], CHANNELS, 1000.0, 1010.0)
    assert seen and set(seen) == {("repeatable read", "on")}

def test_marking_records_the_time_range_the_bucket_held(store):
    retire(store, 1, "DELETING")
    with psycopg.connect(store.dsn) as conn:
        assert conn.execute("SELECT first_wall,last_wall FROM bucket WHERE no=1").fetchone() == (1001.0, 1001.0)
