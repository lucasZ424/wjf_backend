"""D23: the 10 s watermark and its commit barrier (终版 §5).

Why it matters: a watermark says "history is complete up to here".  It may only be backed by proven
communication, and it may never become durable before the data it claims to cover.
"""

import asyncio
import time

from test_run_segments import owner_and_groups
from test_source_validity import WALL0, at, data

from htsg import records as r
from htsg.commitq import CommitQueue


def watermarks(rec):
    return [s for g in rec.commits for s in g.states if s.kind == "WATERMARK"]


def run_seconds(owner, start, end, keepalive_until=None):
    for second in range(start, end):
        wall, mono = at(float(second))
        if keepalive_until is None or second <= keepalive_until:
            owner.accept(r.InputBatch("ua", 1, r.KIND_UA_NOTIFY, wall, mono))
        owner.tick(wall, mono)


def test_a_source_that_never_answered_gets_no_watermark():
    owner, rec = owner_and_groups()
    for second in range(101, 160):
        owner.tick(*at(float(second)))
    assert watermarks(rec) == [], "a running timer alone proves nothing about the source"
    assert owner.history_status("ua")["coverage"] == "OBSERVATIONS_ONLY"


def test_the_watermark_is_the_last_proven_communication_never_now():
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    run_seconds(owner, 102, 140, keepalive_until=121)  # the source falls silent after second 121
    marks = watermarks(rec)
    assert marks, "healthy communication produces a watermark every 10 s"
    assert all(m.detail["covered_wall"] <= at(121.0)[0] for m in marks), "never beyond proven communication"
    assert max(m.detail["covered_wall"] for m in marks) >= at(112.0)[0]
    gaps = [b.at_mono - a.at_mono for a, b in zip(marks, marks[1:], strict=False)]
    assert all(g >= 10 for g in gaps), "about every 10 s, not more often"
    covered = [m.detail["covered_wall"] for m in marks]
    assert covered == sorted(set(covered)), "a watermark only ever moves forward"
    last = len(marks)
    run_seconds(owner, 140, 200, keepalive_until=0)
    assert len(watermarks(rec)) == last, "a silent source stops advancing the watermark entirely"


def test_a_watermark_group_commits_only_after_everything_before_it(archive_db_factory):
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    run_seconds(owner, 102, 125)
    store = archive_db_factory()
    store.initialize(owner.config)
    store.bind_run("run", owner.config.config_id, WALL0)

    class FlakyDisk:
        """Fails the data group a few times: the watermark queued behind it must wait."""

        def __init__(self):
            self.failures = {rec.commits[2].group_id: 3}
            self.written = []

        def write_group(self, group):
            from htsg.archive_router import RouteResult

            if self.failures.get(group.group_id, 0) > 0:
                self.failures[group.group_id] -= 1
                return RouteResult("FAILED", "primary", "OperationalError")
            outcome = store.write_group(group)
            self.written.append(group.order)
            return RouteResult(outcome.status, "primary")

    disk = FlakyDisk()
    queue = CommitQueue(disk, 1000, retry_delay=(0.01, 0.02))
    for group in rec.commits:
        assert queue.offer(group)

    async def settle():
        task = asyncio.create_task(queue.run())
        deadline = time.monotonic() + 15
        while queue.depth and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await queue.stop(1.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(settle())
    assert disk.written == sorted(disk.written), "strictly in the owner's order, despite the failures"
    with store.session() as conn:
        orders = {x["order_no"] for x in conn.execute("SELECT order_no FROM commit_group")}
        for row in conn.execute("SELECT group_order FROM watermark").fetchall():
            earlier = {g.order for g in rec.commits if g.order < row["group_order"]}
            assert earlier <= orders, "every group before a stored watermark is stored too"


def test_when_storage_stalls_for_a_minute_only_the_confirmed_watermark_is_claimed():
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    run_seconds(owner, 102, 121)
    emitted_then = watermarks(rec)
    assert emitted_then
    # the first two watermark groups are confirmed; storage then stalls while 60 s of operation go by
    marks_groups = [g for g in rec.commits if g.kind == r.GROUP_WATERMARK]
    owner.committed(marks_groups[0])
    confirmed = owner.history_status("ua")["confirmed_watermark"]
    assert confirmed == marks_groups[0].states[0].detail["covered_wall"]
    run_seconds(owner, 121, 181)
    assert len(watermarks(rec)) > len(emitted_then), "new watermarks keep being produced and queued"
    status = owner.history_status("ua")
    assert status["confirmed_watermark"] == confirmed, "nothing unconfirmed is claimed as stored history"
    assert at(181.0)[0] - status["confirmed_watermark"] > 50, "the uncertain tail is NOT limited to 10 s"
    assert status["coverage"] == "WATERMARK"
    owner.committed([g for g in rec.commits if g.kind == r.GROUP_WATERMARK][-1])
    assert owner.history_status("ua")["confirmed_watermark"] > confirmed


def test_without_any_confirmed_watermark_only_stored_points_are_presented():
    owner, rec = owner_and_groups()
    data(owner, 101.0)
    run_seconds(owner, 102, 150)
    status = owner.history_status("ua")
    assert status["confirmed_watermark"] is None and status["coverage"] == "OBSERVATIONS_ONLY"
