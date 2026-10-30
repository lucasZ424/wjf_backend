"""D21: ordered group queue and commit confirmation (终版 §5, §16.2-1).

Why it matters: a watermark that overtakes its data would certify history that is not there; a full queue
that silently overwrites would hide loss; and a slow database must never slow the realtime state.
"""

import asyncio
import threading
import time

import pytest
from owner_support import make_owner, poll
from test_archive_write import make_group

from htsg import records as r
from htsg.archive_router import ArchiveRouter, RouteResult
from htsg.commitq import CommitQueue


class ScriptedRouter:
    """Stands in for the router: scripted outcomes per group id, optional blocking inside the write."""

    def __init__(self, script=None):
        self.script = script or {}
        self.written = []  # (group_id, status) in the order the writer touched them
        self.gate = None  # threading.Event the write waits for

    def write_group(self, group):
        if self.gate is not None:
            self.gate.wait(10)
        status = (self.script.get(group.group_id) or ["COMMITTED"]).pop(0) if self.script.get(group.group_id) else "COMMITTED"
        self.written.append((group.group_id, status))
        return RouteResult(status, "primary", "x" if status not in ("COMMITTED", "ALREADY") else None)


def g(order, kind=r.GROUP_BATCH):
    return make_group(order=order, kind=kind)


async def drain(queue, until, timeout=10.0):
    task = asyncio.create_task(queue.run())
    deadline = time.monotonic() + timeout
    while not until() and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    await queue.stop(1.0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_data_and_watermark_share_one_fifo_and_nothing_overtakes_a_failing_head():
    router = ScriptedRouter({"run1/g1": ["FAILED", "COMMITTED"]})
    queue = CommitQueue(router, 10, retry_delay=(0.01, 0.02))
    assert queue.offer(g(1)) and queue.offer(g(2, r.GROUP_WATERMARK)) and queue.offer(g(3))
    await drain(queue, lambda: len(router.written) >= 4)
    assert router.written == [
        ("run1/g1", "FAILED"), ("run1/g1", "COMMITTED"), ("run1/g2", "COMMITTED"), ("run1/g3", "COMMITTED"),
    ]  # fmt: skip


async def test_a_full_queue_refuses_visibly_and_never_overwrites_what_it_holds():
    router = ScriptedRouter()
    queue = CommitQueue(router, 3)
    offers = [queue.offer(g(n)) for n in (1, 2, 3, 4)]
    assert offers == [True, True, True, False]
    assert [x.group_id for x in queue.queue] == ["run1/g1", "run1/g2", "run1/g3"]
    assert queue.stats["rejected"] == 1 and queue.depth == 3
    await drain(queue, lambda: len(router.written) == 3)
    assert queue.offer(g(5)), "room again once the writer has settled groups"


async def test_a_slow_database_does_not_slow_realtime_state_and_is_not_announced_until_durable():
    router = ScriptedRouter()
    router.gate = threading.Event()
    confirmed = []
    queue = CommitQueue(router, 500, on_committed=lambda group, result: confirmed.append(group.order))
    owner, rec_ = make_owner()
    owner.commit = queue.offer
    task = asyncio.create_task(queue.run())
    slowest = 0.0
    for tick in range(1, 60):  # the database is stuck for the whole loop
        started = time.perf_counter()
        owner.accept(poll(tick, {"b1": 0.5 + tick * 0.01, "b2": 0.6}))
        slowest = max(slowest, time.perf_counter() - started)
        await asyncio.sleep(0)
    assert owner.channel_views()["b1"]["value"] == pytest.approx(0.5 + 59 * 0.01), "the live view kept up with every round"
    assert slowest < 0.05, f"intake never waited for storage ({slowest:.3f}s)"
    assert confirmed == [] and queue.stats["highest_committed_order"] == 0, "nothing is announced as stored"
    assert queue.depth >= 58
    router.gate.set()
    deadline = time.monotonic() + 10
    while queue.depth and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    await queue.stop(1.0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert confirmed == sorted(confirmed) and len(confirmed) >= 59, "announced in order once durable"


async def test_a_refused_group_is_dropped_and_reported_without_blocking_later_groups():
    refused = []
    router = ScriptedRouter({"run1/g1": ["REFUSED"]})
    queue = CommitQueue(router, 10, on_refused=lambda group, reason: refused.append((group.group_id, reason)))
    queue.offer(g(1))
    queue.offer(g(2))
    await drain(queue, lambda: len(router.written) >= 2)
    assert refused == [("run1/g1", "x")] and router.written[-1] == ("run1/g2", "COMMITTED")
    assert queue.stats["refused"] == 1


async def test_a_voided_group_is_let_go_of_by_the_router_that_may_remember_it():
    """An unknown commit is remembered by the router for its retries; a group dropped as void gets none, so it must say so."""

    class Remembering(ScriptedRouter):
        released = []

        def release(self, group_id):
            self.released.append(group_id)

    router = Remembering()
    queue = CommitQueue(router, 10, is_void=lambda group: group.order == 1)
    queue.offer(g(1))
    queue.offer(g(2))
    await drain(queue, lambda: len(router.written) >= 1)
    assert router.released == ["run1/g1"] and router.written == [("run1/g2", "COMMITTED")]
    assert queue.stats["voided"] == 1


async def test_unknown_outcomes_are_retried_for_the_same_group():
    router = ScriptedRouter({"run1/g1": ["UNKNOWN", "UNKNOWN", "COMMITTED"]})
    queue = CommitQueue(router, 10, retry_delay=(0.01, 0.02))
    queue.offer(g(1))
    await drain(queue, lambda: len(router.written) >= 3)
    assert [s for _, s in router.written] == ["UNKNOWN", "UNKNOWN", "COMMITTED"] and queue.stats["unknown"] == 2


async def test_against_real_postgres_groups_become_durable_in_offer_order(archive_db_factory):
    from test_archive_router import Disks, config

    cfg = config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    router = ArchiveRouter(cfg, primary, emergency, disk_usage=Disks())
    router.initialize()
    router.bind_run("run1", cfg.config_id, 999.0)
    seen = []

    def confirm(group, result):
        seen.append((group.group_id, primary.group_committed(group.group_id, group.bucket)))  # durable at announcement time

    queue = CommitQueue(router, 50, on_committed=confirm)
    for n in range(1, 8):
        assert queue.offer(make_group(order=n, config_id=cfg.config_id))
    await drain(queue, lambda: len(seen) == 7)
    assert seen == [(f"run1/g{n}", True) for n in range(1, 8)]
    with primary.session() as conn:
        order = [x["order_no"] for x in conn.execute("SELECT order_no FROM commit_group ORDER BY committed_at,order_no")]
    assert order == list(range(1, 8))
