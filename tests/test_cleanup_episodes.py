"""One cleanup is one cleanup (修复轮09 第3项; 终版 §13.6 自检与报警: 实测删除周期).

Why it matters: the page shows the measured deletion period - the time between the starts of the last two cleanups - and
the next deletion is judged against it.  A cleanup starts when the normal disk reaches the start line and ends below the stop
line; a long transaction that postpones it, a round's budget running out, or a restart in the middle are still the same
cleanup.  Counting by gaps between log entries split one postponed cleanup into two and halved the period shown.  And every
range taken away is in the management log once, whole, also when its deletion was finished by a later round.
"""

import psycopg
import pytest
from test_rolling_delete import Usage, build, deleted, rig, states

from htsg import buckets as b


@pytest.fixture
def pair(archive_db_factory):
    return archive_db_factory("primary", prepare=False), archive_db_factory("emergency", prepare=False)


def actions(store):
    with psycopg.connect(store.dsn) as conn:
        return [
            (x[0], x[1])
            for x in conn.execute(
                "SELECT action,target FROM management_log WHERE action LIKE 'ROLLING_DELETE%%' ORDER BY time,id"
            )
        ]


def long_reader(store):
    """A long query of the history page: it holds back DETACH ... CONCURRENTLY, so the deletion is postponed."""
    reader = psycopg.connect(store.dsn)
    reader.execute("BEGIN ISOLATION LEVEL REPEATABLE READ")
    reader.execute("SELECT count(*) FROM observation")
    return reader


def test_a_postponed_cleanup_finished_in_a_later_round_is_one_start_and_one_end_and_its_range_is_logged_whole(tmp_path, pair):
    m, cfg = rig(tmp_path, pair, keep=16, statement_seconds=2)
    build(pair[0], cfg, 20, without_baseline=(2,))  # buckets 1 and 2 go together: 3 begins with the next baseline
    reader = long_reader(pair[0])
    m.cycle()
    assert states(pair[0])[1] == states(pair[0])[2] == "DELETING", "the whole range is marked, then the deletion waits"
    reader.close()
    m.cycle()
    assert deleted(pair[0]) == {1, 2, 3, 4}
    assert actions(pair[0]) == [
        ("ROLLING_DELETE_START", "cleanup"),
        ("ROLLING_DELETE", "buckets 1-2"),  # finished by the later round, logged once and whole
        ("ROLLING_DELETE", "buckets 3-3"),
        ("ROLLING_DELETE", "buckets 4-4"),
        ("ROLLING_DELETE_END", "cleanup"),
    ]
    assert m.snapshot.health["retention"]["deletion_period_s"] is None, "one cleanup so far: no period yet"


def test_the_period_is_the_time_between_the_starts_of_the_last_two_cleanups(tmp_path, pair):
    m, cfg = rig(tmp_path, pair, keep=18)
    build(pair[0], cfg, 20)
    m.cycle()
    m.disk_usage.keep = 16  # the disk fills up again: the next cleanup
    m.cycle()
    with psycopg.connect(pair[0].dsn) as conn:
        starts = [x[0] for x in conn.execute("SELECT time FROM management_log WHERE action='ROLLING_DELETE_START' ORDER BY time")]
    assert len(starts) == 2
    assert m.snapshot.health["retention"]["deletion_period_s"] == starts[1] - starts[0]


def test_a_cleanup_going_on_when_the_process_stopped_goes_on_to_the_stop_line_after_the_restart_as_the_same_one(tmp_path, pair):
    first, cfg = rig(tmp_path, pair, keep=16, statement_seconds=2)
    build(pair[0], cfg, 20)
    reader = long_reader(pair[0])
    first.cycle()  # started, then postponed - and the process ends here
    reader.close()
    primary, emergency = pair
    # after the restart the disk is between the lines: above the stop line, below the start line
    again = b.Maintenance(cfg, {"primary": primary.dsn, "emergency": emergency.dsn}, tmp_path / "n.json",
                          disk_usage=Usage(primary, 16, full=0.85))  # fmt: skip
    again.cycle()
    assert deleted(pair[0]) == {1, 2, 3, 4}, "the cleanup is finished down to the stop line, not dropped half way"
    kinds = [a for a, _ in actions(pair[0])]
    assert kinds.count("ROLLING_DELETE_START") == 1 and kinds[-1] == "ROLLING_DELETE_END"
