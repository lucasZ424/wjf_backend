"""D20: whole-group routing (终版 §5 C3, §13.5, §16.2-1).

Why it matters: the old router halved oversized batches and the emergency route threw derived results away.
A group split across databases, or retried elsewhere after an unknown commit, would corrupt history.
"""

from collections import namedtuple

import psycopg
import pytest
from archive_support import said_and_held, samples
from owner_support import small_config
from test_archive_write import CONFIG, RUN, make_group

from htsg import archive_router
from htsg.archive import WriteOutcome
from htsg.archive_router import ArchiveRouter

Usage = namedtuple("Usage", "total used free")
GIB = 1024**3


class Disks:
    """Injectable free-space readings: a real drive is never filled to test a threshold."""

    def __init__(self):
        self.used = {}  # path -> fraction used

    def __call__(self, path):
        total = 1000 * GIB
        used = self.used.get(path, 0.10)
        return Usage(total, int(total * used), int(total * (1 - used)))


def config(**storage):
    payload = small_config().model_dump(mode="json")
    payload["storage"] = {
        "primary_path": "D:/a", "emergency_path": "D:/b", **storage,
    }  # fmt: skip
    return type(CONFIG).model_validate(payload)


@pytest.fixture
def rig(archive_db_factory):
    class Rig:
        pass

    rig = Rig()
    rig.disks = Disks()
    rig.cfg = config()
    rig.primary, rig.emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    rig.router = ArchiveRouter(rig.cfg, rig.primary, rig.emergency, disk_usage=rig.disks)
    rig.router.initialize()
    rig.router.bind_run(RUN, rig.cfg.config_id, 999.0)
    return rig


def rows(store, table="sample"):  # inputs and derived results alike (修复轮14)
    with store.session() as conn:
        return conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_an_over_budget_group_is_refused_whole_never_split(archive_db_factory):
    disks = Disks()
    cfg = config(batch_budget_bytes=2000)
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    router = ArchiveRouter(cfg, primary, emergency, disk_usage=disks)
    router.initialize()
    result = router.write_group(make_group(n_obs=30, config_id=cfg.config_id))
    assert (result.status, result.reason) == ("REFUSED", "WRITE_BATCH_BUDGET")
    assert rows(primary) == rows(emergency) == 0 and rows(primary, "commit_group") == 0


def test_a_group_larger_than_the_old_128_record_write_batch_is_one_atomic_transaction(rig):
    result = rig.router.write_group(make_group(n_obs=400, config_id=rig.cfg.config_id))
    assert result.status == "COMMITTED" and result.target == "primary"
    assert rows(rig.primary) == 400 + 1 and rows(rig.primary, "commit_group") == 1, "400 inputs and the result"


def test_the_emergency_route_keeps_derived_results_with_their_inputs(rig):
    rig.disks.used["D:/a"] = 0.96  # the normal database is over the 95 % switch point
    result = rig.router.write_group(make_group(config_id=rig.cfg.config_id))
    assert result.status == "COMMITTED" and result.target == "emergency" and rig.router.mode == "EMERGENCY"
    with rig.emergency.session() as conn:
        kinds = [x["kind"] for x in samples(conn)]
    assert (kinds.count("raw"), kinds.count("derived")) == (3, 1), "derived results are not dropped"
    assert rows(rig.emergency, "fault_record") == 1 and rows(rig.primary) == 0


def test_each_group_lands_complete_in_exactly_one_database(rig):
    for order, used in ((1, 0.2), (2, 0.2), (3, 0.97), (4, 0.97), (5, 0.40)):
        rig.disks.used["D:/a"] = used
        assert rig.router.write_group(make_group(order=order, config_id=rig.cfg.config_id)).durable
    with rig.primary.session() as p, rig.emergency.session() as e:
        per_db = {}
        for name, conn in (("primary", p), ("emergency", e)):
            said, held = said_and_held(conn)
            assert said == held, f"{name}: every row there belongs to a group there, every group's rows are there"
            for row in conn.execute("SELECT group_id FROM commit_group").fetchall():
                per_db.setdefault(row["group_id"], {})[name] = 1
    assert all(len(where) == 1 for where in per_db.values()), "no group is spread over two databases"
    assert {g: next(iter(w)) for g, w in per_db.items()} == {
        "run1/g1": "primary", "run1/g2": "primary", "run1/g3": "emergency", "run1/g4": "emergency",
        "run1/g5": "primary",  # usage fell to 40 %, below the 90 % recovery line: back to the normal database
    }  # fmt: skip


def test_an_unknown_commit_is_verified_in_the_same_database_and_never_rewritten_elsewhere(rig):
    real = rig.primary.write_group
    state = {"calls": 0}

    def commit_then_lose_the_reply(group):
        state["calls"] += 1
        real(group)  # it really became durable ...
        return WriteOutcome("UNKNOWN", "OperationalError")  # ... but the caller never learned that

    rig.primary.write_group = commit_then_lose_the_reply
    result = rig.router.write_group(make_group(config_id=rig.cfg.config_id))
    assert result.status == "COMMITTED" and result.target == "primary", "verified by stable id"
    assert state["calls"] == 1 and rows(rig.primary, "commit_group") == 1
    assert rows(rig.emergency, "commit_group") == 0, "no blind rewrite into another database"


def test_a_commit_that_really_did_not_happen_stays_unknown_and_is_retried_on_the_same_target(rig):
    real = rig.primary.write_group
    attempts = []

    def fails_once(group):
        attempts.append(1)
        if len(attempts) == 1:
            return WriteOutcome("UNKNOWN", "OperationalError")  # nothing was written
        return real(group)

    rig.primary.write_group = fails_once
    first = rig.router.write_group(make_group(config_id=rig.cfg.config_id))
    assert first.status == "UNKNOWN" and first.target == "primary"
    assert rows(rig.emergency, "commit_group") == 0
    second = rig.router.write_group(make_group(config_id=rig.cfg.config_id))  # what the writer does: same group, same target
    assert second.status == "COMMITTED" and second.target == "primary"
    assert rows(rig.primary) == 3 + 1 and rig.router.verify("run1/g1", 1) == "primary"


def lose_the_reply(rig):
    """The primary takes the group but its answer is lost, and the check by id cannot be made either (the database is away
    for that moment): the commit stays unknown.  Returns the real write, for `answer_again`."""
    real = rig.primary.write_group

    def write(group):
        real(group)  # it really became durable ...
        return WriteOutcome("UNKNOWN", "OperationalError")  # ... but nobody was told

    def away(group_id, bucket):
        raise psycopg.OperationalError("the database is away")

    rig.primary.write_group, rig.primary.group_committed = write, away
    return real


def answer_again(rig, real):
    rig.primary.write_group = real
    del rig.primary.group_committed  # the class's own method again: the database answers


def test_a_group_of_unknown_commit_goes_back_to_its_own_database_even_when_the_disks_now_point_elsewhere(rig):
    """终版 §5 C3, "retried against the same target": the retry used to route again.  The first attempt became durable in the
    primary and nobody could tell; before the retry the primary volume filled up, and a NEW group would now go to the
    emergency database - this one must not, or the same group is stored twice."""
    group = make_group(config_id=rig.cfg.config_id)
    real = lose_the_reply(rig)
    first = rig.router.write_group(group)
    assert (first.status, first.target) == ("UNKNOWN", "primary") and rig.router.pinned == {group.group_id: "primary"}
    rig.disks.used["D:/a"] = 0.96  # the route would now say: emergency
    answer_again(rig, real)
    second = rig.router.write_group(group)  # what the writer does: the same group again
    assert (second.status, second.target) == ("ALREADY", "primary")
    assert rows(rig.primary, "commit_group") == 1 and rows(rig.emergency, "commit_group") == 0, "one copy, in the first database"
    assert rig.router.pinned == {} and rig.router.mode == "PRIMARY", "released with the confirmation; the route was not touched"
    later = rig.router.write_group(make_group(order=2, config_id=rig.cfg.config_id))
    assert (later.target, rig.router.mode) == ("emergency", "EMERGENCY"), "other groups follow the disks as before"


def test_a_group_of_unknown_commit_is_not_sent_elsewhere_while_its_database_keeps_failing_to_say(rig):
    group = make_group(config_id=rig.cfg.config_id)
    real = lose_the_reply(rig)
    assert rig.router.write_group(group).status == "UNKNOWN"
    rig.disks.used["D:/a"] = 0.96
    for _ in range(3):  # still away: every attempt is unknown again, none of them goes anywhere else
        again = rig.router.write_group(group)
        assert (again.status, again.target) == ("UNKNOWN", "primary")
    assert rows(rig.emergency, "commit_group") == 0 and rig.router.pinned == {group.group_id: "primary"}
    answer_again(rig, real)
    assert rig.router.write_group(group).status == "ALREADY" and rig.router.pinned == {}


def test_a_group_is_remembered_with_its_database_until_it_is_settled(rig):
    """终版 §13.5 (修复轮09 审核第4条): a certain failure is remembered too - its retry keeps the database it was sent to."""
    assert rig.router.write_group(make_group(config_id=rig.cfg.config_id)).durable and rig.router.pinned == {}
    real = rig.primary.write_group
    rig.primary.write_group = lambda group: WriteOutcome("FAILED", "OperationalError")  # certain: rolled back, nothing is there
    group = make_group(order=2, config_id=rig.cfg.config_id)
    assert rig.router.write_group(group).status == "FAILED" and rig.router.pinned == {group.group_id: "primary"}
    rig.primary.write_group = real
    assert rig.router.write_group(group).durable and rig.router.pinned == {}, "settled: nothing left to remember"


def test_the_memory_of_groups_of_unknown_commit_is_bounded_and_never_drops_one_to_make_room(rig, monkeypatch):
    """Forgetting the oldest let its retry be routed again - to another database, and stored twice - without a word.  A NEW
    group of unknown commit that finds the memory full is refused out loud instead; the groups already remembered keep their
    database.  (The refused one may well be durable - its reply was lost - which is why it is a reported gap, not a denial.)"""
    monkeypatch.setattr(archive_router, "PINNED_LIMIT", 2)
    real = lose_the_reply(rig)
    group = {order: make_group(order=order, config_id=rig.cfg.config_id) for order in (1, 2, 3, 4)}
    for order in (1, 2):
        assert rig.router.write_group(group[order]).status == "UNKNOWN"
    third = rig.router.write_group(group[3])
    assert (third.status, third.reason, third.durable) == ("REFUSED", "PIN_TABLE_FULL", False)
    assert list(rig.router.pinned) == ["run1/g1", "run1/g2"], "nobody was forgotten to make room for the new one"
    assert rig.router.health["pin_table_full"] == 1
    again = rig.router.write_group(group[1])  # a remembered group is still asked of its own database, full table or not
    assert again.status == "UNKNOWN" and rig.router.health["pin_table_full"] == 1
    answer_again(rig, real)
    assert rig.router.write_group(group[1]).status == "ALREADY" and list(rig.router.pinned) == ["run1/g2"]
    lose_the_reply(rig)
    assert rig.router.write_group(group[4]).status == "UNKNOWN", "a released place can be used again"
    assert list(rig.router.pinned) == ["run1/g2", "run1/g4"]


def test_a_group_the_queue_gave_up_frees_its_place_in_the_memory_of_groups_of_unknown_commit(rig, monkeypatch):
    """A voided group is never asked about again; if its place stayed taken, a full table would refuse new groups for ever."""
    monkeypatch.setattr(archive_router, "PINNED_LIMIT", 2)
    lose_the_reply(rig)
    for order in (1, 2):
        assert rig.router.write_group(make_group(order=order, config_id=rig.cfg.config_id)).status == "UNKNOWN"
    assert rig.router.write_group(make_group(order=3, config_id=rig.cfg.config_id)).reason == "PIN_TABLE_FULL"
    rig.router.release("run1/g1")
    rig.router.release("run1/never-pinned")  # a group that was never remembered is no error
    assert rig.router.write_group(make_group(order=3, config_id=rig.cfg.config_id)).status == "UNKNOWN"
    assert list(rig.router.pinned) == ["run1/g2", "run1/g3"]
