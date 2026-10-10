"""D34: one answer from several databases (终版 §13.5).

Why it matters: after a route switch the history of one segment lives in more than one database.  The answer
must show each record once, page without gaps or repeats, and say plainly when a database could not be read.
"""

import psycopg
from test_archive_write import make_group
from test_storage_routing import Rig3

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


def stores(rig, **replace):
    named = {"primary": rig.primary, "emergency": rig.emergency, "external": rig.external}
    named.update(replace)
    return list(named.items())


def test_one_segment_split_over_two_databases_reads_as_one_span_with_a_single_copy_of_each_record(archive_db_factory):
    rig = Rig3(archive_db_factory)
    assert rig.write(primary=0.40).target == "primary"  # group 1: observations 480-482 at wall 1001
    assert rig.write(primary=0.96, external=0.20).target == "external"  # group 2 after the switch, same segment
    rig.primary.write_group(make_group(order=2, config_id=rig.cfg.config_id))  # a duplicate copy of group 2
    result = h.query(stores(rig), CHANNELS, 1000.0, 1010.0)
    (span,) = result["channels"]["ti"]["segments"]
    raw = [x for x in span["rows"] if x["kind"] == "raw"]
    assert len(raw) == len({x["id"] for x in raw}) == 6, "3 + 3 observations, the duplicate collapsed"
    assert [x["batch_seq"] for x in raw] == [1, 1, 1, 2, 2, 2]
    third, fourth = raw[2], raw[3]
    assert third["ended_by"] == "NEXT_CHANGE" and third["until"] == fourth["time"], "the hold links across databases"
    assert result["partial"] is False and result["unavailable"] == []


def test_a_segment_whose_end_sits_in_another_database_is_not_open_for_a_window_after_that_end(archive_db_factory):
    rig = Rig3(archive_db_factory)
    rig.write(primary=0.40)  # segment 1 starts in the normal database, its values at wall 1001
    closing = r.StateRecord(
        r.state_id("run1", "ua", 99), "run1", "ua", 1, "SEG_END", 1010.0, 110.0,
        {"reason": "ARCHIVE_GAP", "end_wall": 1005.0},
    )  # fmt: skip
    rig.external.write_group(
        make_group(order=2, config_id=rig.cfg.config_id, observations=(), results=(), states=(closing,), faults=())
    )  # the close was routed to the removable database: there it is a row without a start
    across = h.query(stores(rig), CHANNELS, 1003.0, 1004.0)
    (span,) = across["channels"]["ti"]["segments"]
    assert span["end_wall"] == 1005.0 and span["rows"][-1]["until"] == 1005.0, "the end is found in the other database"
    after = h.query(stores(rig), CHANNELS, 1006.0, 1010.0)
    assert after["channels"]["ti"]["segments"] == [], "nothing is held through time the segment no longer covers"


def test_a_database_that_cannot_be_read_makes_the_answer_partial_not_silently_complete(archive_db_factory):
    rig = Rig3(archive_db_factory)
    rig.write(primary=0.40)
    rig.write(primary=0.96, external=0.20)
    result = h.query(stores(rig, primary=Offline(rig.primary)), CHANNELS, 1000.0, 1010.0)
    assert result["partial"] is True and result["unavailable"] == ["primary"]
    assert result["coverage"] == "PARTIAL"
    assert all(v is None for v in result["coverage_end"].values()), "no source is claimed complete"
    raw = [x for s in result["channels"]["ti"]["segments"] for x in s["rows"] if x["kind"] == "raw"]
    assert {x["batch_seq"] for x in raw} == {2}, "what is readable is shown; what is not, is not guessed"


def test_a_late_sample_alone_is_not_taken_for_a_complete_watermark(archive_db_factory):
    rig = Rig3(archive_db_factory)
    rig.write(primary=0.40)
    rig.write(primary=0.96, external=0.20)
    complete = h.query(stores(rig), CHANNELS, 1000.0, 1010.0)
    assert complete["coverage_end"]["ua"] == 1000.5, "both databases read: the watermark of the segment is known"
    partial = h.query(stores(rig, primary=Offline(rig.primary)), CHANNELS, 1000.0, 1010.0)
    assert partial["coverage_end"] == {"ua": None}


def test_disabling_new_writes_to_the_removable_database_does_not_hide_what_it_already_holds(archive_db_factory):
    rig = Rig3(archive_db_factory)
    rig.write(primary=0.96, external=0.20)  # lands in the removable database
    assert rig.where() == {"run1/g1": ["external"]}
    payload = rig.cfg.model_dump(mode="json")
    payload["storage"]["external_enabled"] = False  # new writes are switched off ...
    disabled = type(rig.cfg).model_validate(payload)
    router = ArchiveRouter(disabled, rig.primary, rig.emergency, rig.external, disk_usage=rig.disks)
    router.initialize()
    router.bind_run("run1", disabled.config_id, 999.0)
    result = router.write_group(make_group(order=2, config_id=disabled.config_id))
    assert result.target != "external", "... and nothing is written there any more"
    found = h.query(router.stores, CHANNELS, 1000.0, 1010.0)
    assert {x["batch_seq"] for s in found["channels"]["ti"]["segments"] for x in s["rows"]} == {1, 2}


def test_fault_paging_covers_every_record_once_across_databases_even_with_equal_times(archive_db_factory):
    rig = Rig3(archive_db_factory)
    # every make_group fault has confirmed_wall 1005.0: only the id breaks the ties
    for n in range(1, 26):
        group = make_group(order=n, config_id=rig.cfg.config_id)
        target = (rig.primary, rig.emergency, rig.external)[n % 3]
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
    rig = Rig3(archive_db_factory)
    for n in range(1, 6):
        rig.write(primary=0.4)
    first = h.list_faults(stores(rig), point="ti", limit=2)
    assert first["next_cursor"]
    import pytest

    with pytest.raises(ValueError):
        h.list_faults(stores(rig), point="other", limit=2, cursor=first["next_cursor"])


def test_fault_listing_reports_an_unreadable_database_and_stays_usable(archive_db_factory):
    rig = Rig3(archive_db_factory)
    rig.write(primary=0.4)
    rig.write(primary=0.96, external=0.2)
    page = h.list_faults(stores(rig, external=Offline(rig.external)), limit=10)
    assert page["partial"] and page["unavailable"] == ["external"] and page["pagination_incomplete"]
    assert [x["id"] for x in page["items"]] == ["run1/fault/ti/1"]
