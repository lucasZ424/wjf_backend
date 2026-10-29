"""The maintenance thread stops between steps and says whether it ended; a first bucket is usable once it is there
(修复轮09 审核第1、3条; 终版 §13.6 删除过程, plan D7, D8).

Why it matters: a stop that returns while the thread still works lets that thread go on using - and opening again - the
databases the caller is about to close, drop or hand to the next run; a deletion it does not interrupt can hold a stop for
as many buckets as are marked, at up to a minute a statement.  An unfinished deletion loses nothing by waiting: its buckets
stay DELETING and the next round goes on with them.  And a start must not wait for the whole round - seven buckets in two
databases, the statistics - when the bucket a group may be put in is already there: past the 5 s of D7 its groups would be
refused as a gap for nothing.
"""

import threading
import time

from test_rolling_delete import build, pair, rig, states  # noqa: F401

from htsg import buckets as b

RETIRE_SECONDS = 0.6


def slowed(store, started=None):
    """Each bucket taken away takes RETIRE_SECONDS longer, as a long DETACH would."""
    real = store.retire_bucket

    def retire(no):
        if started is not None:
            started.set()
        time.sleep(RETIRE_SECONDS)
        return real(no)

    store.retire_bucket = retire


def marked(store, nos):
    import psycopg

    with psycopg.connect(store.dsn) as conn:
        conn.execute("UPDATE bucket SET state='DELETING' WHERE no=ANY(%s)", (list(nos),))


def test_a_stop_in_the_middle_of_a_deletion_ends_the_thread_and_leaves_the_rest_marked(tmp_path, pair):  # noqa: F811
    m, cfg = rig(tmp_path, pair, keep=100)
    build(pair[0], cfg, 8)
    marked(pair[0], range(1, 6))
    started = threading.Event()
    slowed(m.stores["primary"], started)
    m.start()
    assert started.wait(10), "the round reached the marked buckets"
    assert m.stop(3 * RETIRE_SECONDS) is True, "the thread ended within the wait, after the bucket it was taking away"
    assert not m.thread.is_alive()
    left = {n for n, s in states(pair[0]).items() if s == "DELETING"}
    assert left and left < set(range(1, 6)), "the bucket under way was finished, the others wait for the next round"


def test_finishing_a_marked_range_keeps_to_the_round_budget(tmp_path, pair, monkeypatch):  # noqa: F811
    m, cfg = rig(tmp_path, pair, keep=100)
    build(pair[0], cfg, 8)
    marked(pair[0], range(1, 6))
    slowed(m.stores["primary"])
    monkeypatch.setattr(b, "DELETE_BUDGET", 1.5 * RETIRE_SECONDS)
    m.cycle()
    left = {n for n, s in states(pair[0]).items() if s == "DELETING"}
    assert left and m.snapshot.health["deletion"] == "BUDGET_USED", "a marked range is finished over several rounds"
    m.cycle()
    assert len({n for n, s in states(pair[0]).items() if s == "DELETING"}) < len(left), "and the next round goes on"
    m.stop(1)


def test_a_stop_that_is_not_waited_out_says_so(tmp_path, pair):  # noqa: F811
    m, cfg = rig(tmp_path, pair, keep=100)
    build(pair[0], cfg, 8)
    marked(pair[0], range(1, 3))
    started = threading.Event()
    slowed(m.stores["primary"], started)
    m.start()
    assert started.wait(10)
    assert m.stop(0.01) is False, "a wait that ran out is not an end"
    assert m.stop(5) is True and not m.thread.is_alive()


def test_the_first_bucket_is_usable_before_the_rest_of_the_round_is_done(tmp_path, pair):  # noqa: F811
    primary, emergency = pair
    m, _ = rig(tmp_path, pair, keep=100)
    release, real = threading.Event(), {name: store.prepare_bucket for name, store in m.stores.items()}
    made = []

    def prepare(name):
        def one(no):
            if made:  # the first bucket of each database is made at once, the others wait
                assert release.wait(30)
            real[name](no)
            made.append((name, no))

        return one

    for name, store in m.stores.items():
        store.prepare_bucket = prepare(name)
    m.start()
    try:
        assert m.usable.wait(5), "a bucket a group may be put in is announced as soon as it is there"
        assert 1 in m.snapshot.usable["primary"]
    finally:
        release.set()
        assert m.stop(10) is True
