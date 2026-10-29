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


def shown(result):
    return [(x["value"], x["time"], x["origin"]) for s in result["channels"]["ti"]["segments"] for x in s["rows"]]


def test_a_range_being_deleted_is_marked_no_longer_queryable_and_not_shown(store):
    retire(store, 1, "DELETING")
    result = h.query([("primary", store)], CHANNELS, 1000.0, 1010.0)
    assert 1001.0 not in [t for _, t, _ in shown(result)], "rows of a bucket being deleted are not shown"
    (retired,) = result["retention"]["primary"]["retired"]
    assert (retired["state"], retired["from"], retired["to"]) == ("DELETING", 1001.0, 1001.0)


def test_before_the_boundary_is_deleted_and_inside_the_oldest_kept_bucket_only_possibly_incomplete(store):
    retire(store, 1, "DELETED")
    result = h.query([("primary", store)], CHANNELS, 1000.0, 1010.0)
    kept = result["retention"]["primary"]
    assert kept["deleted_before"] == 1002.5, "the boundary is the earliest baseline of the oldest bucket kept"
    assert [(x["state"], x["from"], x["to"]) for x in kept["retired"]] == [("DELETED", 1001.0, 1001.0)]
    assert [(x["from"], x["to"]) for x in kept["possibly_incomplete"]] == [(1002.0, 1002.5)], "bucket start to its baseline"
    assert shown(result)[0] == (481.0, 1002.0, "RECEIVED")
    assert (481.0, 1002.5, r.ORIGIN_BASELINE) in shown(result), "the baseline counts from its boundary, never before it"


def test_a_window_held_across_the_boundary_takes_its_value_from_the_baseline(store):
    retire(store, 1, "DELETED")
    with store.session() as conn:
        conn.execute("DELETE FROM observation WHERE bucket=2 AND origin='RECEIVED'")  # only the baseline is left there
    result = h.query([("primary", store)], CHANNELS, 1003.0, 1003.5)
    (held,) = [x for s in result["channels"]["ti"]["segments"] for x in s["rows"] if x["held"]]
    assert (held["value"], held["time"], held["origin"]) == (481.0, 1002.5, r.ORIGIN_BASELINE)


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


def test_an_input_in_a_deleted_bucket_is_reported_as_pruned_not_as_missing_without_reason(store):
    basis = h.input_basis([("primary", store)], "run1/ua/b2/r/dt")  # the result of bucket 2
    assert basis["found"] and "BASIS_PRUNED" not in basis["notes"], "nothing was deleted: a missing input is only missing"
    retire(store, 1, "DELETED")
    with store.session() as conn:
        conn.execute("DELETE FROM observation WHERE bucket=2 AND origin='RECEIVED'")
        conn.execute("UPDATE bucket SET first_wall=1000.0,last_wall=1002.0 WHERE no=1")  # say bucket 1 held that time
    basis = h.input_basis([("primary", store)], "run1/ua/b2/r/dt")
    assert "BASIS_PRUNED" in basis["notes"] and basis["complete"] is False, "依据已裁剪: said, not left as a silent gap"


def test_the_label_of_a_baseline_row_in_page_and_csv_is_retention_boundary_baseline():
    from pathlib import Path

    text = (Path(h.__file__).parent / "static/replay.js").read_text(encoding="utf-8")
    assert 'RETENTION_BASELINE: "保留边界基线"' in text


def test_marking_records_the_time_range_the_bucket_held(store):
    retire(store, 1, "DELETING")
    with psycopg.connect(store.dsn) as conn:
        assert conn.execute("SELECT first_wall,last_wall FROM bucket WHERE no=1").fetchone() == (1001.0, 1001.0)


# ---- the input basis (修复轮09 审核第5条): the same isolation as the history page

def being_deleted(store, n):
    """Bucket `n` is DELETING, the state a range is in from its marking until it is gone."""
    with store.session() as conn:
        conn.execute("UPDATE bucket SET state='DELETING' WHERE no=%s", (n,))


def test_the_basis_of_a_result_in_a_bucket_being_deleted_is_not_given(store):
    """The history page no longer shows a bucket being deleted; its results must not come back through the basis page."""
    being_deleted(store, 2)
    basis = h.input_basis([("primary", store)], "run1/ua/b2/r/dt")
    assert basis["found"] is False


def test_the_basis_never_rests_on_a_row_of_a_bucket_being_deleted(store):
    """The result of batch 4 lost its own rows; the state before it is looked for in older batches - not in bucket 2, which
    is being deleted: what was left of it would be reported as a complete basis while it disappears."""
    being_deleted(store, 2)
    with store.session() as conn:
        conn.execute("DELETE FROM observation WHERE bucket=3")
    basis = h.input_basis([("primary", store)], "run1/ua/b4/r/dt")
    rows = basis["inputs"]["ti"]["rows"]
    assert rows and all(row["batch_seq"] == 1 for row in rows), "the newest state outside the bucket being deleted"


def test_the_basis_reads_each_database_in_one_repeatable_read_read_only_transaction(store, monkeypatch):
    opened, real = [], store.session

    def counting():
        opened.append(1)
        return real()

    monkeypatch.setattr(store, "session", counting)
    seen, real_execute = [], psycopg.Connection.execute

    def spying(conn, query, params=None, **kw):
        if "FROM observation" in str(query) or "FROM result" in str(query):
            seen.append(tuple(real_execute(conn, f"SHOW {k}").fetchone()[k] for k in ("transaction_isolation", "transaction_read_only")))
        return real_execute(conn, query, params, **kw)

    monkeypatch.setattr(psycopg.Connection, "execute", spying)
    assert h.input_basis([("primary", store)], "run1/ua/b4/r/dt")["found"]
    assert len(opened) == 1, "one transaction: a deletion going on cannot hand the basis half a bucket"
    assert seen and set(seen) == {("repeatable read", "on")}
