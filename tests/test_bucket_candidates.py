"""History names only the buckets whose time can matter (修复轮09 第1项, 方案甲; 终版 §13.6 保留边界与查询).

Why it matters: every bucket is four partitions.  A statement that does not name its buckets is planned and locked over all
of them: at 480 buckets the history statements ran past the 3 s statement limit and failed, and one query held about 2900
locks of the site's nominal 3840 - a few people looking at history at once could exhaust the lock table and fail the archive
writes on the same instance.  So each bucket keeps the time its rows cover, recorded in the very transaction that writes
them, and the history names only the buckets that time can reach - never fewer: a bucket whose time is not known, rows a
clock set back put into an older time, and the value held in from before the window are all still found.
"""

import time
from contextlib import contextmanager
from dataclasses import replace

import pytest
from test_archive_write import CONFIG, RUN, make_group

from htsg import history as h
from htsg import records as r

CHANNELS = {"ti": "ua"}


def observations(order, walls, point="ti", asserted=None):
    return tuple(
        r.Observation(
            id=r.observation_id(RUN, "ua", order, i), run_id=RUN, source_id="ua", conn_gen=1, seg=1, batch_seq=order,
            item_seq=i, point_id=point, value=float(wall), quality=r.GOOD, status_code=0, received_wall=float(wall),
            received_mono=float(wall), asserted_wall=asserted,
        )
        for i, wall in enumerate(walls)
    )  # fmt: skip


def group(order, bucket, walls, built=None, results=(), asserted=None):
    built = walls[-1] if built is None else built
    return replace(make_group(order=order, bucket=bucket, built_wall=float(built)),
                   observations=observations(order, walls, asserted=asserted), results=results, states=(), faults=())  # fmt: skip


def open_segment(store):
    with store.session() as conn:
        conn.execute(
            "INSERT INTO segment(run_id,source_id,seg,conn_gen,start_wall,start_reason) VALUES(%s,'ua',1,1,0,'CONNECT')", (RUN,)
        )


@pytest.fixture
def store(archive_db_factory):
    s = archive_db_factory()
    s.initialize(CONFIG)
    s.bind_run(RUN, CONFIG.config_id, 0.0)
    open_segment(s)
    return s


def bucket_range(store, no):
    with store.session() as conn:
        row = conn.execute("SELECT data_lo,data_hi FROM bucket WHERE no=%s", (no,)).fetchone()
    return row["data_lo"], row["data_hi"]


def raw(result):
    return [(x["value"], x["held"]) for s in result["channels"]["ti"]["segments"] for x in s["rows"] if x["kind"] == "raw"]


# ---- the range a bucket covers: written with its rows, never narrower than they are


def test_a_written_group_widens_its_buckets_range_in_the_same_transaction_even_when_a_clock_set_it_back(store):
    """The range is what lets a query leave a bucket out; a row outside it would never be shown again."""
    assert store.write_group(group(1, 1, [100.0, 101.0], built=101.5)).durable
    assert bucket_range(store, 1) == (100.0, 101.5), "the rows' time and the group's built time are both inside"
    assert store.write_group(group(2, 1, [102.0, 103.0])).durable
    assert bucket_range(store, 1) == (100.0, 103.0)
    late = group(3, 1, [104.0], asserted=50.0)  # a restated value whose effective time a clock set back puts earlier
    assert store.write_group(late).durable
    assert bucket_range(store, 1) == (50.0, 104.0), "the effective time is what a query filters on, so it is what counts"


def test_a_group_written_twice_or_refused_leaves_the_range_as_its_rows_are(store):
    first = group(1, 1, [100.0])
    assert store.write_group(first).durable
    assert store.write_group(replace(first, built_wall=999.0)).status == "ALREADY"
    assert bucket_range(store, 1) == (100.0, 100.0), "nothing new was stored, so the range does not move"


# ---- what the history finds: the same rows as when every bucket was read


def test_a_window_across_two_buckets_and_the_value_held_in_from_the_bucket_before_are_found(store):
    store.prepare_bucket(2)
    store.prepare_bucket(3)
    assert store.write_group(group(1, 1, [100.0, 110.0])).durable
    assert store.write_group(group(2, 2, [200.0, 210.0])).durable
    assert store.write_group(group(3, 3, [300.0, 310.0])).durable
    result = h.query([("primary", store)], CHANNELS, 205.0, 305.0)
    assert raw(result) == [(200.0, True), (210.0, False), (300.0, False)], "held from bucket 2's start, then both buckets"
    held = h.query([("primary", store)], CHANNELS, 250.0, 260.0)
    assert raw(held) == [(210.0, True)], "nothing inside: the value in effect comes from the bucket before"


def test_rows_a_clock_set_back_put_into_a_newer_bucket_are_found_in_an_older_window(store):
    store.prepare_bucket(2)
    assert store.write_group(group(1, 1, [100.0, 110.0])).durable
    assert store.write_group(group(2, 2, [105.0, 400.0])).durable  # the clock went back to 105 after bucket 2 began
    result = h.query([("primary", store)], CHANNELS, 101.0, 120.0)
    assert sorted(v for v, held in raw(result) if not held) == [105.0, 110.0]  # rows come in batch order


def test_a_bucket_whose_time_is_not_known_is_still_read(store):
    """A bucket written before its range was kept has none: it is read, as every bucket was before."""
    store.prepare_bucket(2)
    assert store.write_group(group(1, 1, [100.0])).durable
    assert store.write_group(group(2, 2, [500.0])).durable
    with store.session() as conn:
        conn.execute("UPDATE bucket SET data_lo=NULL,data_hi=NULL WHERE no=1")
    assert [v for v, held in raw(h.query([("primary", store)], CHANNELS, 90.0, 200.0)) if not held] == [100.0]


def test_a_bucket_whose_range_is_not_known_stays_not_known_when_it_is_written_to_again(store):
    """A bucket holding rows from before its range was kept: a range made of the next group alone would leave the older
    rows out of every query, so its range stays "not known" (修复轮09 第1项 复核)."""
    store.prepare_bucket(2)
    assert store.write_group(group(1, 1, [100.0])).durable
    assert store.write_group(group(2, 2, [500.0])).durable
    with store.session() as conn:
        conn.execute("UPDATE bucket SET data_lo=NULL,data_hi=NULL WHERE no=1")  # as a database laid down before
    assert store.write_group(group(3, 1, [400.0])).durable
    assert bucket_range(store, 1) == (None, None)
    assert [v for v, held in raw(h.query([("primary", store)], CHANNELS, 90.0, 200.0)) if not held] == [100.0]


def test_a_group_written_into_a_bucket_sealed_before_it_was_ever_used_is_found(store):
    """A bucket can be sealed here before its first group arrives (a newer one opened first); written to then, it holds
    rows and must be read like any other."""
    store.prepare_bucket(2)
    assert store.write_group(group(1, 2, [500.0])).durable  # bucket 2 opens first: bucket 1, never used, is sealed
    assert store.write_group(group(2, 1, [100.0])).durable  # a group given to bucket 1 before arrives now
    assert [v for v, held in raw(h.query([("primary", store)], CHANNELS, 90.0, 200.0)) if not held] == [100.0]
    assert bucket_range(store, 1) == (100.0, 100.0)

class Locks:
    """The store a query is given, counting the locks its transaction holds just before it ends."""

    def __init__(self, store):
        self.store, self.held = store, []

    def __getattr__(self, name):
        return getattr(self.store, name)

    @contextmanager
    def session(self):
        with self.store.session() as conn:
            yield conn
            self.held.append(conn.execute("SELECT count(*) AS n FROM pg_locks WHERE pid=pg_backend_pid()").fetchone()["n"])


PER_BUCKET = 250


def baseline(order, bucket, wall, value, marked="SEGMENT"):
    """The retention baseline a bucket begins with: the source's value in force restated at `wall` (§13.6), or - `marked`
    otherwise - the source only marked, as one without a valid segment is."""
    state = r.StateRecord(r.state_id(RUN, "_baseline", order), RUN, "_baseline", 0, "RETENTION_BASELINE", wall, wall,
                          {"set": order, "sources": {"ua": marked}})  # fmt: skip
    restated = () if marked != "SEGMENT" else tuple(
        replace(o, id=r.baseline_id(RUN, order, "o", "ti"), origin=r.ORIGIN_BASELINE, orig_id=o.id, item_seq=2**31 - 1,
                asserted_wall=wall, asserted_mono=wall)
        for o in observations(order, [value])
    )  # fmt: skip
    return replace(make_group(order=order, bucket=bucket, built_wall=wall, kind=r.GROUP_BASELINE, source_id="_baseline"),
                   observations=restated, results=(), states=(state,), faults=())  # fmt: skip


def test_a_baseline_that_only_marked_the_source_does_not_cut_the_search_for_the_value_in_force(store):
    store.prepare_bucket(2)
    assert store.write_group(group(1, 1, [100.0])).durable
    assert store.write_group(baseline(2, 2, 150.0, 100.0, marked="NO_VALID_SEGMENT")).durable
    assert store.write_group(group(3, 2, [300.0])).durable
    assert raw(h.query([("primary", store)], CHANNELS, 200.0, 250.0)) == [(100.0, True)], "found behind the mark"


def fill(store, buckets):
    """`buckets` buckets of PER_BUCKET seconds each, one row a second, written the way the archive writes them: every
    bucket after the first begins with its retention baseline."""
    for n in range(2, buckets + 1):
        store.prepare_bucket(n)
    for n in range(1, buckets + 1):
        if n > 1:
            assert store.write_group(baseline(2 * n, n, (n - 1) * PER_BUCKET + 0.5, float((n - 1) * PER_BUCKET))).durable
        walls = [float((n - 1) * PER_BUCKET + i + 1) for i in range(PER_BUCKET)]
        assert store.write_group(group(2 * n + 1, n, walls)).durable
    with store.session() as conn:
        conn.execute("ANALYZE sample")


def locks_of_a_window(store, start):
    counted = Locks(store)
    h.query([("primary", counted)], CHANNELS, start, start + 100.0)
    return max(counted.held)


def test_a_window_over_480_buckets_is_answered_within_the_bound_and_its_locks_do_not_grow_with_the_buckets(
    archive_db_factory, store
):
    few = store
    fill(few, 4)
    middle = 2 * PER_BUCKET + 50.0
    few_locks = locks_of_a_window(few, middle)

    many = archive_db_factory()
    many.initialize(CONFIG)
    many.bind_run(RUN, CONFIG.config_id, 0.0)
    open_segment(many)
    fill(many, 480)
    start = 240 * PER_BUCKET + 50.0  # inside bucket 241, the value held in from bucket 241's own first rows
    h.query([("primary", many)], CHANNELS, 1.0, 2.0)  # the first plan of a session is not what a page waits for
    started = time.perf_counter()
    result = h.query([("primary", many)], CHANNELS, start, start + 100.0)
    elapsed = time.perf_counter() - started
    rows = raw(result)
    assert rows[0] == (start - 1.0, True) and len(rows) == 102
    many_locks = locks_of_a_window(many, start)
    print(f"\n480 buckets: a 100 s window in {elapsed * 1000:.0f} ms; locks {few_locks} at 4 buckets, {many_locks} at 480")
    assert elapsed < 1.5, f"{elapsed:.2f}s for a 100 s window over 480 buckets"
    assert many_locks <= few_locks + 20, "the locks of a query must not grow with the buckets a site keeps"
