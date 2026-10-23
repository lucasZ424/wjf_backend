"""D32: history inside valid segments (终版 §5, §14).

Why it matters: the replay page must say what was in effect, for how long, and when it is NOT known.
A value must not be stretched across a gap, a disconnect or a segment boundary, nor invented between samples.
"""

import time

from test_run_segments import CONFIG, WALL0, at, data, owner_and_groups, stored

from htsg import history as h
from htsg import records as r

CHANNELS = {"ti": "ua"}


def keepalives(owner, start, stop):
    for second in range(start, stop):
        wall, mono = at(float(second))
        owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, wall, mono))
        owner.tick(wall, mono)


def ask(store, start, end, channels=None, **kw):
    return h.query([("primary", store)], channels or CHANNELS, at(start)[0], at(end)[0], **kw)


def spans(result, pid="ti"):
    return result["channels"][pid]["segments"]


def test_a_window_without_any_change_still_reports_the_value_in_effect_with_its_original_time(archive_db_factory):
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    keepalives(owner, 102, 250)  # healthy and silent about values for 150 s
    store = stored(archive_db_factory, rec)
    result = ask(store, 200, 230)
    (span,) = spans(result)
    (row,) = [x for x in span["rows"] if x["kind"] == "raw"]
    assert row["value"] == 500.0 and row["held"] is True
    assert row["time"] == at(101.0)[0], "its own time, not the window start"
    assert row["ended_by"] == "WATERMARK" and row["until"] >= at(230.0)[0] - 10, "valid up to the proven watermark"
    assert span["covered_until"] is not None


def test_the_value_ends_at_the_earliest_of_next_change_segment_end_gap_and_watermark(archive_db_factory):
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    keepalives(owner, 102, 120)
    data(owner, 120.5, items=(("ti", 510.0),), seq=2)  # (a) next change
    keepalives(owner, 121, 135)
    owner.source_down("ua", 1, "CONNECTION", *at(135.5))  # (b) the segment ends
    store = stored(archive_db_factory, rec)
    rows = [x for x in spans(ask(store, 100, 140))[0]["rows"] if x["kind"] == "raw"]
    first, second = rows
    assert (first["ended_by"], first["until"]) == ("NEXT_CHANGE", second["time"])
    assert second["ended_by"] == "SEGMENT_END" and second["until"] == at(135.5)[0]


def test_an_open_segment_without_a_watermark_has_an_unknown_tail(archive_db_factory):
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    store = stored(archive_db_factory, rec)
    (row,) = [x for x in spans(ask(store, 100, 110))[0]["rows"] if x["kind"] == "raw"]
    assert row["until"] is None and row["ended_by"] is None, "no watermark: only the stored point is presented"


def test_an_archive_gap_stops_the_hold_at_the_start_of_the_gap(archive_db_factory):
    from test_archive_gap import Sink

    sink = Sink()
    owner, rec = owner_and_groups()
    owner.commit, sink.owner = sink, owner
    for stored_before in rec.commits:  # what the first sink took (the run, the first segment) is confirmed durable (R03b)
        owner.committed(stored_before)
    data(owner, 101.0)
    sink.accept = False
    data(owner, 105.0, items=(("ti", 520.0),), seq=2)  # lost
    sink.accept = True
    owner.tick(*at(110.0))
    data(owner, 111.0, items=(("ti", 530.0),), seq=3)
    store = archive_db_factory()
    store.initialize(CONFIG)
    store.bind_run("run", CONFIG.config_id, WALL0)
    for group in [*rec.commits, *sink.groups]:
        store.write_group(group)
    result = ask(store, 100, 120)
    flat = [(x["value"], x["origin"], x["until"], x["ended_by"]) for s in spans(result) for x in s["rows"] if x["kind"] == "raw"]
    first = flat[0]
    assert first[0] == 500.0 and first[2] == at(105.0)[0], "500 is not carried across the gap that began at 105"
    assert first[3] in ("GAP", "SEGMENT_END")
    assert result["gaps"] and result["gaps"][0]["start_wall"] == at(105.0)[0]
    assert (520.0, r.ORIGIN_RECEIVED) not in [(v, o) for v, o, _, _ in flat], "the lost sample is not invented"


def test_nothing_crosses_a_segment_and_a_window_in_a_gap_between_segments_shows_nothing(archive_db_factory):
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    keepalives(owner, 102, 106)
    owner.source_down("ua", 1, "CONNECTION", *at(106.5))  # first segment ends at 106.5
    owner.begin_generation("ua", 2, *at(120.0))  # second segment begins at 120
    data(owner, 121.0, items=(("ti", 540.0), ("to", 450.0)), gen=2, seq=1)
    store = stored(archive_db_factory, rec)
    result = ask(store, 110, 118)  # entirely between the segments
    assert all(not s["rows"] for s in spans(result)), "no value is stretched across the silence"
    both = ask(store, 100, 130)
    segs = spans(both)
    assert len(segs) == 2 and segs[0]["seg"] == 1 and segs[1]["seg"] == 2
    first_ids = {x["id"] for x in segs[0]["rows"]}
    assert not first_ids & {x["id"] for x in segs[1]["rows"]}
    assert segs[0]["rows"][-1]["until"] == at(106.5)[0]


def test_a_restated_value_keeps_its_original_time_and_counts_from_the_restatement(archive_db_factory):
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    owner.tick(*at(104.0))  # expire (3 s)
    wall, mono = at(104.5)
    owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, wall, mono))  # recovery inside the window
    store = stored(archive_db_factory, rec)
    rows = [x for s in spans(ask(store, 100, 110)) for x in s["rows"] if x["kind"] == "raw"]
    original = next(x for x in rows if x["origin"] == r.ORIGIN_RECEIVED)
    restated = next(x for x in rows if x["origin"] == r.ORIGIN_ASSERTION)
    assert restated["orig_id"] == original["id"] and restated["observed_at"] == original["observed_at"]
    assert restated["time"] == at(104.5)[0], "counts from the restatement; the gap 104.0-104.5 is not back-filled"


def test_duplicate_times_inside_one_batch_all_stay(archive_db_factory):
    owner, rec = owner_and_groups()
    data(owner, 101.0, items=(("ti", 1.0), ("ti", 2.0), ("ti", 3.0), ("to", 4.0)))
    store = stored(archive_db_factory, rec)
    rows = [x for x in spans(ask(store, 100, 110))[0]["rows"] if x["kind"] == "raw"]
    assert [(x["value"], x["item_seq"]) for x in rows] == [(1.0, 0), (2.0, 1), (3.0, 2)]
    assert len({x["time"] for x in rows}) == 1, "one instant, three records, ordered by batch position"
    assert [x["ended_by"] for x in rows[:2]] == ["NEXT_CHANGE", "NEXT_CHANGE"]


def test_reaching_the_row_limit_is_reported(archive_db_factory):
    owner, rec = owner_and_groups()
    for n in range(12):
        data(owner, 101.0 + n * 0.01, items=(("ti", 500.0 + n),), seq=n + 1)
    store = stored(archive_db_factory, rec)
    result = h.query([("primary", store)], CHANNELS, at(100)[0], at(110)[0], limit=5)
    assert result["truncated"] and result["channels"]["ti"]["truncated"]
    assert sum(1 for x in spans(result)[0]["rows"] if x["kind"] == "raw") == 5


def two_databases(archive_db_factory, rec, place):
    """The groups of a recorded run, each written to the databases `place(position)` names (0: primary, 1: emergency) - what a
    run that moved between databases leaves behind."""
    stores = [archive_db_factory("primary"), archive_db_factory("emergency")]
    for store in stores:
        store.initialize(CONFIG)
        store.bind_run("run", CONFIG.config_id, WALL0)
    for position, group in enumerate(rec.commits):
        for index in place(position):
            assert stores[index].write_group(group).durable, group.group_id
    return [("primary", stores[0]), ("emergency", stores[1])]


def values_of(result, pid="ti"):
    return [x["value"] for span in spans(result, pid) for x in span["rows"] if x["kind"] == "raw" and not x["held"]]


def series(count, step=0.01, end=None):
    owner, rec = owner_and_groups()
    for n in range(count):
        data(owner, 101.0 + n * step, items=(("ti", 500.0 + n),), seq=n + 1)
    if end is not None:
        owner.shutdown(*at(end))  # the segment is over by then: every row is held until something
    return rec, next(i for i, g in enumerate(rec.commits) if g.observations)  # the groups, and where the data begins


def test_the_row_limit_is_one_limit_over_all_databases_not_one_per_database(archive_db_factory):
    """The audit's must-fix 5.  Each database was asked for its first `limit` rows and the answers were joined: a run that
    had moved between databases showed up to twice the limit, and the channel said nothing about it."""
    rec, first = series(12)
    stores = two_databases(archive_db_factory, rec, lambda i: [0 if i < first + 6 else 1])  # six rows in each database
    result = h.query(stores, CHANNELS, at(100)[0], at(110)[0], limit=8)
    assert values_of(result) == [500.0 + k for k in range(8)], "the first eight rows of the channel, wherever they are stored"
    assert result["truncated"] and result["channels"]["ti"]["truncated"], "neither database reached the limit alone; together they did"


def test_the_rows_that_remain_are_the_first_ones_of_the_channel_whichever_database_holds_them(archive_db_factory):
    rec, first = series(15)
    def place(i):  # rows 0-4 and 10-14 are in the primary, rows 5-9 in the other database
        return [1 if first + 5 <= i < first + 10 else 0]

    stores = two_databases(archive_db_factory, rec, place)
    result = h.query(stores, CHANNELS, at(100)[0], at(110)[0], limit=8)
    assert values_of(result) == [500.0 + k for k in range(8)], "not the first eight of each database joined together"
    assert result["channels"]["ti"]["truncated"]


def test_a_row_stored_in_both_databases_counts_once_toward_the_limit(archive_db_factory):
    rec, _ = series(12)
    stores = two_databases(archive_db_factory, rec, lambda i: [0, 1])  # everything in both
    cut = h.query(stores, CHANNELS, at(100)[0], at(110)[0], limit=8)
    assert values_of(cut) == [500.0 + k for k in range(8)] and cut["truncated"], "twelve rows, not twenty-four, are over a limit of eight"
    whole = h.query(stores, CHANNELS, at(100)[0], at(110)[0], limit=12)
    assert len(values_of(whole)) == 12 and not whole["truncated"], "twelve rows are exactly the limit: nothing is over it"


def test_the_value_held_in_from_before_the_window_is_not_a_row_of_it_and_is_never_cut(archive_db_factory):
    rec, first = series(12, step=1.0)  # one row a second, from 101 to 112
    stores = two_databases(archive_db_factory, rec, lambda i: [0 if i < first + 6 else 1])
    result = h.query(stores, CHANNELS, at(105.5)[0], at(120)[0], limit=3)
    rows = [x for span in spans(result) for x in span["rows"] if x["kind"] == "raw"]
    assert [(x["value"], x["held"]) for x in rows] == [(504.0, True), (505.0, False), (506.0, False), (507.0, False)]
    assert result["channels"]["ti"]["truncated"]


def test_a_long_series_is_answered_quickly_from_the_point_time_index(archive_db_factory):
    store = archive_db_factory()
    store.initialize(CONFIG)
    with store.session() as conn:
        conn.execute(
            "INSERT INTO segment(run_id,source_id,seg,conn_gen,start_wall,start_reason) VALUES('run','ua',1,1,0,'CONNECT')"
        )
        conn.execute(
            "INSERT INTO observation(id,group_id,run_id,source_id,conn_gen,seg,batch_seq,item_seq,point_id,value,quality,"
            "status_code,received_wall,received_mono,origin) "
            "SELECT 'o'||n,'g','run','ua',1,1,n,0,'ti',n::float8,'GOOD',0,n,n,'RECEIVED' FROM generate_series(1,60000) n"
        )
        conn.execute("ANALYZE observation")
    started = time.perf_counter()
    result = h.query([("primary", store)], CHANNELS, 30000.0, 30100.0)
    elapsed = time.perf_counter() - started
    rows = spans(result)[0]["rows"]
    assert rows[0]["held"] and rows[0]["value"] == 29999.0 and len(rows) == 102
    assert elapsed < 1.5, f"{elapsed:.2f}s for a 100 s window in a 60000-row series"


def shown(result, pid="ti"):
    """(value, time, until, ended_by) of every raw row of the window, in order - the value held in from before it is not one."""
    rows = (x for span in spans(result, pid) for x in span["rows"])
    return [(x["value"], x["time"], x["until"], x["ended_by"]) for x in rows if x["kind"] == "raw" and not x["held"]]


def first_data_group(rec):
    return next(i for i, g in enumerate(rec.commits) if g.observations)


def test_a_row_limit_never_lengthens_the_hold_of_the_last_row_shown(archive_db_factory):
    """The review of 2026-10-20.  The row after the limit was dropped before the intervals were worked out, so the last row
    shown was held to the end of its segment as if nothing came after it - and the page and the CSV drew exactly that."""
    rec, _ = series(5, step=1.0, end=200.0)
    store = stored(archive_db_factory, rec)
    whole = shown(ask(store, 100, 210))
    assert [x[3] for x in whole] == ["NEXT_CHANGE"] * 4 + ["SEGMENT_END"], "the premise: only the very last row is held to the end"
    for limit in (1, 2, 3, 4):
        cut = ask(store, 100, 210, limit=limit)
        assert shown(cut) == whole[:limit], f"limit {limit}: every row shown is held exactly as long as when all rows are shown"
        assert shown(cut)[-1][2:] == (whole[limit][1], "NEXT_CHANGE"), "the last one ends where the row left out begins"
        assert cut["channels"]["ti"]["truncated"]
    exact = ask(store, 100, 210, limit=5)
    assert shown(exact) == whole and not exact["channels"]["ti"]["truncated"], "five rows are exactly the limit: nothing is left out"


def test_the_row_that_ends_the_last_one_shown_may_be_in_the_other_database(archive_db_factory):
    """Neither database reaches the limit alone, so the row after it is found only by putting them together."""
    rec, first = series(12, step=1.0, end=200.0)
    for kept in (0, 1):  # the first eight rows in the primary and the rest in the other one, and the other way round
        stores = two_databases(
            archive_db_factory, rec, lambda i, kept=kept: [0, 1] if i < first else [kept if i < first + 8 else 1 - kept]
        )
        whole = shown(h.query(stores, CHANNELS, at(100)[0], at(210)[0]))
        cut = h.query(stores, CHANNELS, at(100)[0], at(210)[0], limit=8)
        assert shown(cut) == whole[:8], f"rows 1-8 in database {kept}"
        assert shown(cut)[-1][2:] == (whole[8][1], "NEXT_CHANGE")
        assert cut["channels"]["ti"]["truncated"]


def test_a_row_left_out_that_is_bad_or_a_failure_record_still_ends_the_hold_of_the_last_row_shown(archive_db_factory):
    """A Good value is not in effect after something that is not Good has come, whether or not the page has room to show it."""
    for what, item in (("a Bad sample", ("ti", None, r.BAD, 0x80350000)), ("a failure record", ("ti", None, r.MISSING + "TIMEOUT", 0))):
        owner, rec = owner_and_groups()
        for n, one in enumerate((("ti", 1.0), ("ti", 2.0), item, ("ti", 4.0))):
            data(owner, 101.0 + n, items=(one,), seq=n + 1)
        owner.shutdown(*at(200.0))
        first = first_data_group(rec)
        single = [("primary", stored(archive_db_factory, rec))]
        split = two_databases(  # the first two rows in the primary, the row that is left out in the other database
            archive_db_factory, rec, lambda i, first=first: [0, 1] if i < first else [0] if i < first + 2 else [1]
        )
        for stores in (single, split):
            whole = shown(h.query(stores, CHANNELS, at(100)[0], at(210)[0]))
            cut = h.query(stores, CHANNELS, at(100)[0], at(210)[0], limit=2)
            assert [x[0] for x in whole] == [1.0, 2.0, None, 4.0], what
            assert shown(cut) == whole[:2], what
            assert shown(cut)[1][2:] == (at(103.0)[0], "NEXT_CHANGE"), f"{what} ends the hold of 2 where it is stored"


def test_a_row_left_out_in_a_later_segment_is_not_shown_and_changes_nothing_in_the_one_before(archive_db_factory):
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    data(owner, 102.0, items=(("ti", 2.0),), seq=2)
    owner.source_down("ua", 1, "CONNECTION", *at(106.5))
    owner.begin_generation("ua", 2, *at(120.0))
    data(owner, 121.0, items=(("ti", 3.0),), gen=2, seq=1)
    owner.shutdown(*at(200.0))
    store = stored(archive_db_factory, rec)
    whole = ask(store, 100, 210)
    cut = ask(store, 100, 210, limit=2)
    assert [x[0] for x in shown(whole)] == [500.0, 2.0, 3.0]
    assert shown(cut) == shown(whole)[:2] and shown(cut)[1][2:] == (at(106.5)[0], "SEGMENT_END")
    assert [s["seg"] for s in spans(cut)] == [1], "the second segment has no row to show: it is not listed empty"


def test_a_crashed_segment_that_nobody_closed_is_held_into_every_later_window_until_the_start_up_close_out_ends_it(
    archive_db_factory,
):
    """Why the start-up close-out exists (R03a).  The process dies without a stop: its last segment is open in the database.  An
    open segment reaches to infinity, so a window long AFTER the crash still shows the value it held (an unknown tail, or a hold
    up to a watermark that lies far before the window).  Once the close-out has ended the segment at its own last evidence the
    dead value is no longer held into windows after it."""
    for silence, why in ((4, "no watermark was written before the crash"), (25, "a watermark was written before the crash")):
        owner, rec = owner_and_groups()
        data(owner, 101.0)
        keepalives(owner, 102, 101 + silence)  # alive and quiet, then the process dies: no shutdown, no segment end
        store = stored(archive_db_factory, rec)
        window = (1000, 1100)  # a quarter of an hour after the crash
        before = [x for s in spans(ask(store, *window)) for x in s["rows"]]
        assert len(before) == 1, f"{why}: the dead segment is shown in a window after it"
        (row,) = before
        assert (row["value"], row["held"]) == (500.0, True), why
        assert row["until"] is None or row["until"] < at(window[0])[0], f"{why}: held to {row['until']}, before the window began"
        closed = store.reconcile_unfinished_runs("the-next-run")
        assert closed and closed[0]["segments_cut"] >= 1, why
        after = [x for s in spans(ask(store, *window)) for x in s["rows"]]
        assert after == [], f"{why}: closed by the close-out, the dead value is not held into the later window"
        ends = {(s["seg"], s["end_reason"]) for s in spans(ask(store, 100, 130))}
        assert ends == {(1, "EXIT_TRUNCATED")}, why
