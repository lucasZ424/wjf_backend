"""D34: one answer from several databases (终版 §13.5).

Why it matters: after a route switch the history of one segment lives in more than one database.  The answer
must show each record once, page without gaps or repeats, and say plainly when a database could not be read.
"""

import psycopg
from test_archive_router import Disks, config
from test_archive_write import make_group

from htsg import history as h
from htsg import records as r
from htsg.archive_router import ArchiveRouter

CHANNELS = {"ti": "ua"}


class Offline:
    """A store that cannot be read."""

    def __init__(self, store):
        self._store = store

    def session(self):
        raise psycopg.OperationalError("offline")

    def __getattr__(self, name):
        return getattr(self._store, name)


class Rig:
    """Normal and emergency databases behind one router, all throw-away: at 95 % the next groups go to emergency."""

    def __init__(self, factory):
        self.disks, self.cfg = Disks(), config()
        self.primary, self.emergency = factory("primary"), factory("emergency")
        self.router = ArchiveRouter(self.cfg, self.primary, self.emergency, disk_usage=self.disks)
        self.router.initialize()
        self.router.bind_run("run1", self.cfg.config_id, 999.0)
        self.n = 0

    def write(self, primary):
        self.disks.used["D:/a"] = primary
        self.n += 1
        return self.router.write_group(make_group(order=self.n, config_id=self.cfg.config_id))


def stores(rig, **replace):
    named = {"primary": rig.primary, "emergency": rig.emergency}
    named.update(replace)
    return list(named.items())


def test_one_segment_split_over_two_databases_reads_as_one_span_with_each_record_once(archive_db_factory):
    """A group is written to one database only (修复轮14: no per-row id merges copies any more), so the rows of both are
    joined as they are: one span, every record once, the hold running on from one database into the other."""
    rig = Rig(archive_db_factory)
    assert rig.write(primary=0.40).target == "primary"  # group 1: observations 480-482 at wall 1001
    assert rig.write(primary=0.96).target == "emergency"  # group 2 after the switch, same segment
    result = h.query(stores(rig), CHANNELS, 1000.0, 1010.0)
    (span,) = result["channels"]["ti"]["segments"]
    raw = [x for x in span["rows"] if x["kind"] == "raw"]
    assert [(x["time"], x["value"]) for x in raw] == [(1001.0, 480.0), (1001.0, 481.0), (1001.0, 482.0),
                                                      (1002.0, 480.0), (1002.0, 481.0), (1002.0, 482.0)]  # fmt: skip
    third, fourth = raw[2], raw[3]
    assert third["ended_by"] == "NEXT_CHANGE" and third["until"] == fourth["time"], "the hold links across databases"
    assert result["partial"] is False and result["unavailable"] == []


def test_a_segment_whose_end_sits_in_another_database_is_not_open_for_a_window_after_that_end(archive_db_factory):
    rig = Rig(archive_db_factory)
    rig.write(primary=0.40)  # segment 1 starts in the normal database, its values at wall 1001
    closing = r.StateRecord(
        r.state_id("run1", "ua", 99), "run1", "ua", 1, "SEG_END", 1010.0, 110.0,
        {"reason": "ARCHIVE_GAP", "end_wall": 1005.0},
    )  # fmt: skip
    rig.emergency.write_group(
        make_group(order=2, config_id=rig.cfg.config_id, observations=(), results=(), states=(closing,), faults=())
    )  # the close was routed to the emergency database: there it is a row without a start
    across = h.query(stores(rig), CHANNELS, 1003.0, 1004.0)
    (span,) = across["channels"]["ti"]["segments"]
    assert span["end_wall"] == 1005.0 and span["rows"][-1]["until"] == 1005.0, "the end is found in the other database"
    after = h.query(stores(rig), CHANNELS, 1006.0, 1010.0)
    assert after["channels"]["ti"]["segments"] == [], "nothing is held through time the segment no longer covers"


def test_a_database_that_cannot_be_read_makes_the_answer_partial_not_silently_complete(archive_db_factory):
    rig = Rig(archive_db_factory)
    rig.write(primary=0.40)
    rig.write(primary=0.96)
    result = h.query(stores(rig, primary=Offline(rig.primary)), CHANNELS, 1000.0, 1010.0)
    assert result["partial"] is True and result["unavailable"] == ["primary"]
    assert result["coverage"] == "PARTIAL"
    assert all(v is None for v in result["coverage_end"].values()), "no source is claimed complete"
    raw = [x for s in result["channels"]["ti"]["segments"] for x in s["rows"] if x["kind"] == "raw"]
    assert {x["time"] for x in raw} == {1002.0}, "what is readable is shown; what is not, is not guessed"


def test_a_late_sample_alone_is_not_taken_for_a_complete_watermark(archive_db_factory):
    rig = Rig(archive_db_factory)
    rig.write(primary=0.40)
    rig.write(primary=0.96)
    complete = h.query(stores(rig), CHANNELS, 1000.0, 1010.0)
    assert complete["coverage_end"]["ua"] == 1000.5, "both databases read: the watermark of the segment is known"
    partial = h.query(stores(rig, primary=Offline(rig.primary)), CHANNELS, 1000.0, 1010.0)
    assert partial["coverage_end"] == {"ua": None}


def test_fault_paging_covers_every_record_once_across_databases_even_with_equal_times(archive_db_factory):
    rig = Rig(archive_db_factory)
    # every make_group fault has confirmed_wall 1005.0: only the id breaks the ties
    for n in range(1, 26):
        group = make_group(order=n, config_id=rig.cfg.config_id)
        target = (rig.primary, rig.emergency)[n % 2]
        assert target.write_group(group).durable
        if n % 4 == 0:  # a duplicate delivery of the same fault into a second database
            assert rig.primary.write_group(group).durable
    pages, cursor = [], None
    while True:
        page = h.list_faults(stores(rig), limit=7, cursor=cursor)
        pages.append([x["id"] for x in page["items"]])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    flat = [i for page in pages for i in page]
    assert len(flat) == len(set(flat)) == 25, "no repeat, no loss"
    assert flat == sorted(flat, reverse=True), "a stable order, newest first, id as the tie-breaker"
    assert [len(p) for p in pages] == [7, 7, 7, 4]


def test_a_cursor_from_another_query_is_refused(archive_db_factory):
    rig = Rig(archive_db_factory)
    for n in range(1, 6):
        rig.write(primary=0.4)
    first = h.list_faults(stores(rig), point="ti", limit=2)
    assert first["next_cursor"]
    import pytest

    with pytest.raises(ValueError):
        h.list_faults(stores(rig), point="other", limit=2, cursor=first["next_cursor"])


def test_fault_listing_reports_an_unreadable_database_and_stays_usable(archive_db_factory):
    rig = Rig(archive_db_factory)
    rig.write(primary=0.4)
    rig.write(primary=0.96)
    page = h.list_faults(stores(rig, emergency=Offline(rig.emergency)), limit=10)
    assert page["partial"] and page["unavailable"] == ["emergency"] and page["pagination_incomplete"]
    assert [x["id"] for x in page["items"]] == ["run1/fault/ti/1"]
