"""R03a / Q02: a run that exits without a stop closes each segment at ITS OWN evidence, and across databases only what
no database has closed.

Real PostgreSQL.  Why it matters (终版 §5): history holds a value until the end of its segment or the confirmed
watermark.  If every segment of a crashed run were cut at the run's latest evidence, a source that went quiet early
would be shown as valid until another source's last sample; and a run that ended normally in the emergency database
would be called an abnormal exit by the database it had left, with its segments cut short.  The evidence of a segment
is its WATERMARK (细则 5.1 as revised 2026-10-23: the close-out reads only the run, segment and watermark tables, so it takes
the same time for a crashed run of any size); a segment without one ends at its start - nothing is made up.  Observations,
restated values and every other record are no evidence: those after the cut stay visible in history and hold nothing beyond
themselves.
"""

from contextlib import contextmanager

import psycopg
import pytest
from owner_support import small_config

from htsg import history as h
from htsg import records as r
from htsg.archive import ArchiveStore
from htsg.archive_router import ArchiveRouter

CONFIG = small_config()
RUN = "crashed"
POINT = {"ua": "ti", "mb": "b1"}
CHANNELS = {"ti": "ua", "b1": "mb"}


class Unreachable:
    """A store that cannot be reached at all."""

    def __init__(self, store):
        self._store = store

    def initialize(self, config):
        raise psycopg.OperationalError("offline")

    def session(self):
        raise psycopg.OperationalError("offline")

    def __getattr__(self, name):
        return getattr(self._store, name)


class ReconcileFailsOnce:
    """Readable for the cross-store fact pass, then fails this store's first reconciliation write."""

    def __init__(self, store):
        self._store, self.failed = store, False

    def reconcile_unfinished_runs(self, *args, **kwargs):
        if not self.failed:
            self.failed = True
            raise psycopg.OperationalError("reconciliation interrupted")
        return self._store.reconcile_unfinished_runs(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._store, name)


class Records:
    """Writes the records of one run into stores by hand, one group per call, in the order of the calls."""

    def __init__(self, *stores):
        for store in stores:
            store.initialize(CONFIG)
            store.bind_run(RUN, CONFIG.config_id, 999.0)
        self.order = 0

    def put(self, store, source, *, seg=1, start=None, seen=(), restated=(), bad=(), extreme=(), missing=(), poll=None,
            mark=None, end=None, run_end=None):  # fmt: skip
        """`seen`: walls of valid observations; `restated`: walls a valid value was re-stated at; `bad`, `extreme`,
        `missing`: walls of observations that are no evidence; `poll`: wall of a state event; `mark`: watermark;
        `end`: (wall, reason) of a segment end; `run_end`: wall of the end of the run."""
        self.order += 1
        n, point = self.order, POINT[source]
        observations = []

        def add(wall, **fields):
            fields = {"value": 480.0, "quality": r.GOOD, "extreme": False, **fields}
            observations.append(
                r.Observation(
                    id=r.observation_id(RUN, source, n, len(observations)), run_id=RUN, source_id=source, conn_gen=1, seg=seg,
                    batch_seq=n, item_seq=len(observations), point_id=point, status_code=0, received_wall=wall,
                    received_mono=wall - 900.0, **fields,
                )
            )  # fmt: skip

        for wall in seen:
            add(wall)
        for wall in restated:
            add(wall - 5.0, origin=r.ORIGIN_ASSERTION, orig_id="earlier", asserted_wall=wall, asserted_mono=wall - 900.0)
        for wall in bad:
            add(wall, value=None, quality=r.BAD)
        for wall in extreme:
            add(wall, extreme=True)
        for wall in missing:
            add(wall, value=None, quality="MISSING_TIMEOUT")
        states = []

        def state(kind, wall, **detail):
            states.append(r.StateRecord(r.state_id(RUN, source, n * 10 + len(states)), RUN, source, seg, kind, wall, wall - 900.0, detail))

        if start is not None:
            state("SEG_START", start, conn_gen=1, reason="CONNECT")
        if poll is not None:
            state("POLL", poll)
        if mark is not None:
            state("WATERMARK", mark, seg=seg, covered_wall=mark)
        if end is not None:
            state("SEG_END", end[0], end_wall=end[0], reason=end[1])
        if run_end is not None:
            state("RUN_END", run_end, ended_wall=run_end, reason="NORMAL")
        group = r.CommitGroup(
            r.group_id(RUN, n), n, RUN, source, r.GROUP_BATCH, 1000.0 + n, tuple(observations), (), tuple(states), (), 1
        )  # fmt: skip
        assert store.write_group(group).durable


def segments(store):
    with store.session() as conn:
        rows = conn.execute("SELECT source_id,seg,start_wall,end_wall,end_reason FROM segment ORDER BY source_id,seg").fetchall()
    return {(x["source_id"], x["seg"]): (x["start_wall"], x["end_wall"], x["end_reason"]) for x in rows}


def run_of(store):
    with store.session() as conn:
        return conn.execute("SELECT ended_wall,end_reason FROM run WHERE run_id=%s", (RUN,)).fetchone()


def spy_on(store, seen):
    """Records the text of every statement the store runs from now on."""
    original = store.session

    class Spy:
        def __init__(self, conn):
            self._conn = conn

        def execute(self, query, *args, **kwargs):
            seen.append(str(query))
            return self._conn.execute(query, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    @contextmanager
    def session():
        with original() as conn:
            yield Spy(conn)

    store.session = session


def restart(primary, emergency, run_id="next"):
    router = ArchiveRouter(CONFIG, primary, emergency)
    router.initialize(run_id)
    return router


def ask(*named, start=1000.0, end=1030.0):
    return h.query(list(named), CHANNELS, start, end)


def last_row(result, channel):
    (span,) = result["channels"][channel]["segments"]
    return span["rows"][-1]


@pytest.fixture
def stores(archive_db_factory):
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    return primary, emergency, Records(primary, emergency)


# ------------------------------------------------------------------------------- a crash, one database


def test_each_segment_ends_at_its_own_watermark_and_the_run_ends_at_the_latest_of_its_start_and_its_watermarks(stores):
    primary, emergency, rec = stores
    rec.put(primary, "ua", start=1000.0, seen=[1001.0], mark=1002.0)
    rec.put(primary, "mb", start=1000.0, seen=[1001.0, 1010.0], mark=1010.0, poll=1015.0)
    restart(primary, emergency)
    assert segments(primary) == {
        ("mb", 1): (1000.0, 1010.0, "EXIT_TRUNCATED"),
        ("ua", 1): (1000.0, 1002.0, "EXIT_TRUNCATED"),
    }, "the quiet source is not carried to the busy one's last sample"
    assert tuple(run_of(primary).values()) == (1010.0, "ABNORMAL_EXIT"), "the run's end: its latest watermark (a POLL at 1015 is none)"
    result = ask(("primary", primary))
    assert last_row(result, "ti")["until"] == 1002.0, "the hold stops at the segment's own evidence"
    assert result["coverage_end"] == {"ua": 1002.0, "mb": 1010.0}, "no source's coverage is lifted by another"


def test_a_segment_without_a_watermark_ends_at_its_start_and_its_sample_holds_nothing_beyond_itself(stores):
    primary, emergency, rec = stores
    rec.put(primary, "ua", start=1000.0, seen=[1001.0])
    rec.put(primary, "mb", start=1000.0, seen=[1010.0], mark=1010.0)
    restart(primary, emergency)
    assert segments(primary)[("ua", 1)] == (1000.0, 1000.0, "EXIT_TRUNCATED")
    result = ask(("primary", primary))
    assert last_row(result, "ti")["time"] == last_row(result, "ti")["until"] == 1001.0, "only the sample itself is kept"
    assert result["coverage_end"]["ua"] is None, "no watermark, no certain coverage"


def test_samples_after_the_watermark_stay_visible_and_none_of_them_holds_beyond_itself(stores):
    primary, emergency, rec = stores
    rec.put(primary, "ua", start=1000.0, seen=[1001.0], mark=1005.0)
    rec.put(primary, "ua", seen=[1007.0])
    rec.put(primary, "ua", seen=[1008.0])
    rec.put(primary, "mb", start=1000.0, seen=[1020.0], mark=1020.0)
    restart(primary, emergency)
    assert segments(primary)[("ua", 1)] == (1000.0, 1005.0, "EXIT_TRUNCATED")
    result = ask(("primary", primary))
    (span,) = result["channels"]["ti"]["segments"]
    assert [(x["time"], x["until"]) for x in span["rows"]] == [(1001.0, 1005.0), (1007.0, 1007.0), (1008.0, 1008.0)], (
        "the watermark vouches up to 1005; what lies after it is shown, each holding only itself - 1007 is not held to 1008"
    )
    assert result["coverage_end"]["ua"] == 1005.0, "what is certain is the watermark, not the samples after it"


def test_a_bad_sample_after_the_last_evidence_is_shown_but_holds_nothing_and_the_valid_one_before_it_holds_to_the_cut(stores):
    """A bad sample is no evidence, so it can lie beyond the cut: it stays visible, its hold never starts before itself."""
    primary, emergency, rec = stores
    rec.put(primary, "ua", start=1000.0, seen=[1001.0], mark=1002.0)
    rec.put(primary, "ua", bad=[1004.0])
    rec.put(primary, "mb", start=1000.0, seen=[1020.0], mark=1020.0)
    restart(primary, emergency)
    assert segments(primary)[("ua", 1)] == (1000.0, 1002.0, "EXIT_TRUNCATED")
    (span,) = ask(("primary", primary))["channels"]["ti"]["segments"]
    assert [(x["time"], x["valid"], x["until"]) for x in span["rows"]] == [(1001.0, True, 1002.0), (1004.0, False, 1004.0)]


def test_a_re_stated_value_is_no_evidence_either_it_stays_visible_and_holds_nothing_beyond_itself(stores):
    primary, emergency, rec = stores
    rec.put(primary, "ua", start=1000.0, seen=[1001.0], restated=[1009.0])
    rec.put(primary, "mb", start=1000.0, seen=[1020.0], mark=1020.0)
    restart(primary, emergency)
    assert segments(primary)[("ua", 1)] == (1000.0, 1000.0, "EXIT_TRUNCATED")
    (span,) = ask(("primary", primary))["channels"]["ti"]["segments"]
    assert [(x["time"], x["until"]) for x in span["rows"]] == [(1001.0, 1001.0), (1009.0, 1009.0)]


def test_records_that_only_show_the_owner_was_running_are_no_evidence_so_such_a_segment_ends_at_its_start(stores):
    """A bad, an extreme and a missing observation, and a state event, prove the owner was alive - not that the source
    was talking and valid."""
    primary, emergency, rec = stores
    rec.put(primary, "ua", start=1000.0, bad=[1003.0], extreme=[1004.0], missing=[1005.0], poll=1006.0)
    rec.put(primary, "mb", start=1000.0, seen=[1020.0], mark=1020.0)
    restart(primary, emergency)
    assert segments(primary)[("ua", 1)] == (1000.0, 1000.0, "EXIT_TRUNCATED")


# ------------------------------------------------------------------ data the old rule already wrote


def test_the_close_out_reads_no_observation_result_state_or_fault_row_so_a_run_of_any_size_closes_in_the_same_time(stores):
    """R03a: the start-up close-out used to scan these tables, whose size grows with the crashed run: beyond about 6-8M rows its
    statements passed the 3 s limit, nothing was closed, and the next start did the same.  The close-out is checked by its
    structure, not by a timer: whatever it reads, it never reads them."""
    primary, emergency, rec = stores
    rec.put(primary, "ua", start=1000.0, seen=[1001.0, 1002.0, 1003.0], bad=[1004.0], poll=1005.0, mark=1003.0)
    rec.put(primary, "mb", start=1000.0, seen=[1010.0], mark=1010.0)
    statements = []
    for store in (primary, emergency):
        spy_on(store, statements)
    restart(primary, emergency)
    assert statements, "the close-out did run"
    big = ("observation", "result", "state_event", "fault_record")
    touched = [q for q in statements if any(table in q for table in big)]
    assert touched == [], f"read or wrote a table that grows with the run: {touched}"
    assert any("watermark" in q for q in statements), "it works from the watermarks"
    assert segments(primary) == {("mb", 1): (1000.0, 1010.0, "EXIT_TRUNCATED"), ("ua", 1): (1000.0, 1003.0, "EXIT_TRUNCATED")}


def test_a_run_that_cannot_be_closed_now_leaves_the_others_closed_and_the_database_unreported_as_unavailable(stores, monkeypatch):
    """D (2026-10-23): one transaction per run; a failure is DEFERRED with its reason, not a database failure, and it does not
    hold the others back.  The next start closes the one that was left."""
    primary, emergency, rec = stores
    rec.put(primary, "ua", start=1000.0, seen=[1001.0], mark=1002.0)
    with primary.session() as conn:  # a second crashed run, which is closed although the first is not
        conn.execute("INSERT INTO run(run_id,config_id,started_wall) VALUES('other',%s,1500)", (CONFIG.config_id,))
        conn.execute("INSERT INTO segment(run_id,source_id,seg,conn_gen,start_wall,start_reason) VALUES('other','ua',1,1,1500,'CONNECT')")
    original = ArchiveStore._close_run

    def failing_for_the_first(conn, run, *args):
        if run["run_id"] == RUN:
            raise psycopg.errors.QueryCanceled("too slow")
        return original(conn, run, *args)

    monkeypatch.setattr(ArchiveStore, "_close_run", staticmethod(failing_for_the_first))
    router = restart(primary, emergency)
    assert router.health["reconcile"] == "DEFERRED:primary:QueryCanceled"
    assert router.health["primary"] == "OK", "the database is fine; one close-out was deferred"
    assert tuple(run_of(primary).values()) == (None, None), "the run that failed is left as it was"
    with primary.session() as conn:
        other = conn.execute("SELECT ended_wall,end_reason FROM run WHERE run_id='other'").fetchone()
    assert (other["ended_wall"], other["end_reason"]) == (1500.0, "ABNORMAL_EXIT"), "the other run was closed all the same"
    assert [x["run_id"] for x in router.health["primary_closed_runs"]] == ["other"]
    monkeypatch.undo()
    again = restart(primary, emergency, "later")
    assert "reconcile" not in again.health and tuple(run_of(primary).values()) == (1002.0, "ABNORMAL_EXIT")


@pytest.mark.parametrize("mark", [1002.0, None], ids=["with-a-watermark", "without-one"])
def test_a_segment_cut_by_the_old_rule_is_read_within_its_watermark_and_the_stored_rows_are_not_rewritten(stores, mark):
    """No history is migrated (整改方案 R03a): the reading limits what an over-long cut would have shown."""
    primary, emergency, rec = stores
    rec.put(primary, "ua", start=1000.0, seen=[1001.0], mark=mark)
    with primary.session() as conn:
        conn.execute("UPDATE run SET ended_wall=1020,end_reason='ABNORMAL_EXIT',last_evidence_wall=1020")
        conn.execute("UPDATE segment SET end_wall=1020,end_reason='EXIT_TRUNCATED'")
    before = segments(primary)
    result = ask(("primary", primary))
    expected = 1002.0 if mark else 1001.0
    assert last_row(result, "ti")["until"] == expected
    assert result["coverage_end"]["ua"] == mark
    restart(primary, emergency)
    assert segments(primary) == before and tuple(run_of(primary).values()) == (1020.0, "ABNORMAL_EXIT")


# ---------------------------------------------------------------------- the run spans two databases


def cross_database_run(primary, emergency, rec, *, normal_stop):
    """Starts in the normal database, goes on in the emergency one; stopped, or not."""
    rec.put(primary, "ua", start=1000.0, seen=[1001.0], mark=1002.0)
    if normal_stop:
        rec.put(emergency, "ua", seen=[1010.0], mark=1011.0, end=(1011.0, "SHUTDOWN"), run_end=1012.0)
    else:
        rec.put(emergency, "ua", seen=[1010.0], mark=1011.0)


def test_a_run_that_ended_normally_in_the_emergency_database_is_not_an_abnormal_exit_of_the_one_it_left(stores):
    primary, emergency, rec = stores
    cross_database_run(primary, emergency, rec, normal_stop=True)
    router = restart(primary, emergency)
    assert tuple(run_of(primary).values()) == (None, None), "the database the run left is not asked to judge it"
    assert tuple(run_of(emergency).values()) == (1012.0, "NORMAL")
    assert segments(primary)[("ua", 1)][1:] == (None, None), "a segment closed elsewhere is not cut here"
    assert router.health["primary_closed_runs"] == [] and router.health["emergency_closed_runs"] == []
    result = ask(("primary", primary), ("emergency", emergency))
    (span,) = result["channels"]["ti"]["segments"]
    assert (span["end_wall"], span["end_reason"]) == (1011.0, "SHUTDOWN"), "the end and its reason come from one record"
    assert span["rows"][-1]["until"] == 1011.0 and result["partial"] is False


def test_the_same_run_after_a_crash_is_cut_at_the_evidence_of_the_segment_from_both_databases(stores):
    primary, emergency, rec = stores
    cross_database_run(primary, emergency, rec, normal_stop=False)
    router = restart(primary, emergency)
    assert tuple(run_of(primary).values()) == (1002.0, "ABNORMAL_EXIT")
    assert tuple(run_of(emergency).values()) == (1011.0, "ABNORMAL_EXIT"), "each database keeps the run's evidence it holds"
    assert segments(primary)[("ua", 1)][1:] == (1011.0, "EXIT_TRUNCATED"), "the segment's evidence is in both"
    assert router.health["primary_closed_runs"][0]["segments_cut"] == 1
    result = ask(("primary", primary), ("emergency", emergency))
    assert last_row(result, "ti")["until"] == 1011.0 and result["coverage_end"]["ua"] == 1011.0


def test_nothing_is_closed_while_a_bound_database_cannot_be_read_and_the_next_start_closes_it_right(stores):
    primary, emergency, rec = stores
    cross_database_run(primary, emergency, rec, normal_stop=True)
    router = restart(primary, Unreachable(emergency))
    assert router.health["emergency"] == "UNAVAILABLE"
    assert router.health["reconcile"] == "PENDING_UNREADABLE:emergency"
    assert tuple(run_of(primary).values()) == (None, None) and segments(primary)[("ua", 1)][1:] == (None, None)
    partial = ask(("primary", primary), ("emergency", Unreachable(emergency)))
    assert partial["partial"] is True and partial["unavailable"] == ["emergency"]
    assert all(v is None for v in partial["coverage_end"].values()), "no source is claimed complete"
    again = restart(primary, emergency, "later")
    assert "reconcile" not in again.health
    assert tuple(run_of(primary).values()) == (None, None) and segments(primary)[("ua", 1)][1:] == (None, None)
    (span,) = ask(("primary", primary), ("emergency", emergency))["channels"]["ti"]["segments"]
    assert (span["end_wall"], span["end_reason"]) == (1011.0, "SHUTDOWN")


def test_cross_database_crash_reconciliation_resumes_after_one_store_failed_halfway(stores):
    """R03a/Q02: an ABNORMAL_EXIT written by the first store is not a real RUN_END that may permanently shield the
    second store when the first reconciliation pass was interrupted between stores."""
    primary, emergency, rec = stores
    rec.put(primary, "ua", start=1000.0, seen=[1001.0], mark=1002.0)
    rec.put(emergency, "mb", start=1000.0, seen=[1010.0], mark=1011.0)
    interrupted = ReconcileFailsOnce(emergency)
    first = restart(primary, interrupted)
    assert tuple(run_of(primary).values()) == (1002.0, "ABNORMAL_EXIT")
    assert tuple(run_of(emergency).values()) == (None, None)
    assert first.health["reconcile"] == "DEFERRED:emergency:OperationalError", "said to be deferred, with the reason"
    assert first.health["emergency"] == "OK", "the database that was readable and writable is not reported unavailable"

    second = restart(primary, emergency, "later")
    assert tuple(run_of(emergency).values()) == (1011.0, "ABNORMAL_EXIT")
    assert segments(emergency)[("mb", 1)][1:] == (1011.0, "EXIT_TRUNCATED")
    assert second.health["emergency_closed_runs"][0]["run_id"] == RUN


# ---------------------------------------------------------------------------- merging segment rows


def seg(end_wall, end_reason, start_wall=1000.0):
    return {
        "run_id": RUN, "source_id": "ua", "seg": 1, "conn_gen": 1, "start_wall": start_wall, "start_reason": "CONNECT",
        "end_wall": end_wall, "end_reason": end_reason,
    }  # fmt: skip


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ((1020.0, "EXIT_TRUNCATED"), (1011.0, "SHUTDOWN"), (1011.0, "SHUTDOWN")),
        ((1011.0, "SHUTDOWN"), (1020.0, "EXIT_TRUNCATED"), (1011.0, "SHUTDOWN")),
        ((1011.0, "SHUTDOWN"), (1015.0, "CLOCK_JUMP"), (1015.0, "CLOCK_JUMP")),
        ((1015.0, "CLOCK_JUMP"), (1011.0, "SHUTDOWN"), (1015.0, "CLOCK_JUMP")),
        ((1020.0, "EXIT_TRUNCATED"), (1030.0, "EXIT_TRUNCATED"), (1030.0, "EXIT_TRUNCATED")),
        ((None, None), (1011.0, "SHUTDOWN"), (1011.0, "SHUTDOWN")),
        ((1011.0, "SHUTDOWN"), (None, None), (1011.0, "SHUTDOWN")),
    ],
    ids=["cut-then-end", "end-then-cut", "ends-latest-wins", "ends-latest-wins-reversed", "two-cuts", "open-then-end", "end-then-open"],
)
def test_merged_segments_take_the_end_and_its_reason_from_one_record_and_a_real_end_beats_a_cut(first, second, expected):
    """A real end is never overruled by a cut; among real ends the latest wins, with ITS reason (整改方案 R03a)."""
    reads = [h.StoreRead("a", segments=[seg(*first)]), h.StoreRead("b", segments=[seg(*second, start_wall=None)])]
    merged = h._merge_segments(reads)[(RUN, "ua", 1)]
    assert (merged["end_wall"], merged["end_reason"]) == expected and merged["start_wall"] == 1000.0
