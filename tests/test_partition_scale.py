"""History over many buckets (修复轮09 09g; 终版 §13.6: about 232 buckets kept at the lowest band, more when values change less).

Why it matters: every bucket is four partitions, and a query plans over all of them.  The count of buckets a site keeps
must not make the history page unusable: a 100 s window in a 60000-row series spread over 240 buckets is held to the bound
the single-table series of the same size has (test_history_query), and the answer is the same.
"""

import time

from htsg import history as h

BUCKETS, PER_BUCKET = 240, 250


def test_a_window_over_240_buckets_is_answered_within_the_bound_of_one_table(archive_db_factory):
    store = archive_db_factory()
    for n in range(2, BUCKETS + 1):
        store.prepare_bucket(n)
    with store.session() as conn:
        conn.execute(
            "INSERT INTO segment(run_id,source_id,seg,conn_gen,start_wall,start_reason) VALUES('run','ua',1,1,0,'CONNECT')"
        )
        conn.execute(
            "INSERT INTO observation(bucket,id,group_id,run_id,source_id,conn_gen,seg,batch_seq,item_seq,point_id,value,"
            "quality,status_code,received_wall,received_mono,origin) "
            "SELECT 1+(n-1)/%s,'o'||n,'g','run','ua',1,1,n,0,'ti',n::float8,'GOOD',0,n,n,'RECEIVED' "
            "FROM generate_series(1,%s) n",
            (PER_BUCKET, BUCKETS * PER_BUCKET),
        )
        conn.execute("ANALYZE observation")
    assert len(store.bucket_catalog()) == BUCKETS
    h.query([("primary", store)], {"ti": "ua"}, 1.0, 2.0)  # the first plan of a session is not what a page waits for
    started = time.perf_counter()
    result = h.query([("primary", store)], {"ti": "ua"}, 30000.0, 30100.0)
    elapsed = time.perf_counter() - started
    rows = [x for s in result["channels"]["ti"]["segments"] for x in s["rows"]]
    assert rows[0]["held"] and rows[0]["value"] == 29999.0 and len(rows) == 102
    print(f"\n240 buckets, 960 partitions: a 100 s window in {elapsed * 1000:.0f} ms")
    assert elapsed < 1.5, f"{elapsed:.2f}s for a 100 s window over {BUCKETS} buckets"
