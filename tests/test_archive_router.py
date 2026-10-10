"""D20: whole-group routing (终版 §5 C3, §13.5, §16.2-1).

Why it matters: the old router halved oversized batches and the emergency route threw derived results away.
A group split across databases, or retried elsewhere after an unknown commit, would corrupt history.
"""

from collections import namedtuple

import pytest
from owner_support import small_config
from test_archive_write import CONFIG, RUN, make_group

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
        "primary_path": "D:/a", "emergency_path": "D:/b", "external_path": "D:/c", **storage,
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


def rows(store, table="observation"):
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
    assert rows(rig.primary) == 400 and rows(rig.primary, "commit_group") == 1


def test_the_emergency_route_keeps_derived_results_with_their_inputs(rig):
    rig.disks.used["D:/a"] = 0.96  # the normal database is over the 95 % switch point
    result = rig.router.write_group(make_group(config_id=rig.cfg.config_id))
    assert result.status == "COMMITTED" and result.target == "emergency" and rig.router.mode == "EMERGENCY"
    assert rows(rig.emergency) == 3 and rows(rig.emergency, "result") == 1, "derived results are not dropped"
    assert rows(rig.emergency, "fault_record") == 1 and rows(rig.primary) == 0


def test_each_group_lands_complete_in_exactly_one_database(rig):
    for order, used in ((1, 0.2), (2, 0.2), (3, 0.97), (4, 0.97), (5, 0.40)):
        rig.disks.used["D:/a"] = used
        assert rig.router.write_group(make_group(order=order, config_id=rig.cfg.config_id)).durable
    with rig.primary.session() as p, rig.emergency.session() as e:
        per_db = {}
        for name, conn in (("primary", p), ("emergency", e)):
            for row in conn.execute(
                "SELECT group_id,count(*) AS n FROM observation GROUP BY group_id"
            ).fetchall():
                per_db.setdefault(row["group_id"], {})[name] = row["n"]
    assert all(len(where) == 1 for where in per_db.values()), "no group is spread over two databases"
    assert {g: next(iter(w)) for g, w in per_db.items()} == {
        "run1/g1": "primary", "run1/g2": "primary", "run1/g3": "emergency", "run1/g4": "emergency",
        "run1/g5": "primary",  # usage fell to 40 %, below the 70 % recovery line: back to the normal database
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
    assert rows(rig.primary) == 3 and rig.router.verify("run1/g1") == "primary"
