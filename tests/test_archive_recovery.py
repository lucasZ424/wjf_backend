"""D25: atomic recovery with bounded queueing (终版 §5 C2).

Why it matters: a recovery that is only partly stored, or that is declared done before it is durable, would
show history as complete while a gap is still open; unbounded waiting would exhaust memory in an outage.
"""

import asyncio
import time

from owner_support import FixedBucket
from test_archive_gap import Sink, build, lose_the_archive
from test_run_segments import CONFIG
from test_source_validity import WALL0, at, data, keepalive

from htsg import records as r
from htsg.archive_router import ArchiveRouter, RouteResult
from htsg.commitq import CommitQueue
from htsg.owner import Owner


def test_a_second_overflow_while_waiting_retakes_the_baseline_and_memory_stays_bounded():
    owner, sink, rec = build()
    owner.pending_limit = 3
    data(owner, 101.0)
    lose_the_archive(owner, sink)
    owner.tick(*at(103.0))
    s = owner.sources["ua"]
    assert s.archive == "RECOVERING"
    first_recovery = s.recovery_order
    for n in range(10):  # the recovery is not confirmed yet: later groups wait, but only a few of them
        data(owner, 104.0 + n, items=(("ti", 510.0 + n),), seq=10 + n)
        assert len(s.pending) <= 3
    assert owner.health["pending_overflows"] >= 1 and s.archive == "LOST"
    owner.committed(sink.groups[-1] if sink.groups[-1].order == first_recovery else next(g for g in sink.groups if g.order == first_recovery))
    assert s.archive == "LOST", "the first unit is confirmed, yet the archive was lost again after it: not recovered"
    owner.tick(*at(114.0))
    newest = sink.groups[-1]
    assert newest.kind == r.GROUP_RECOVERY and newest.order != first_recovery
    assert {o.point_id: o.value for o in newest.observations}["ti"] == 519.0, "the baseline is taken again, now"
    assert owner.is_void(newest) is False, "the recovery unit itself is the one group allowed through"


def test_a_refused_recovery_unit_is_never_declared_recovered_and_is_tried_again():
    owner, sink, _ = build()
    data(owner, 101.0)
    lose_the_archive(owner, sink)
    owner.tick(*at(103.0))
    recovery = sink.groups[-1]
    owner.refused(recovery, "WRITE_BATCH_BUDGET")
    assert owner.sources["ua"].archive == "LOST" and owner.health["archive_recoveries"] == 0
    assert owner.history_status("ua")["archive"] == "LOST" and owner.history_status("ua")["gap_since"] is not None
    owner.tick(*at(104.0))
    again = sink.groups[-1]
    assert again.order != recovery.order and again.kind == r.GROUP_RECOVERY, "tried again with a fresh unit"
    owner.committed(again)
    assert owner.sources["ua"].archive == "OK" and owner.health["archive_recoveries"] == 1
    assert owner.history_status("ua")["gap_since"] is None


def test_groups_that_waited_for_the_recovery_follow_it_in_order_once_it_is_durable():
    owner, sink, _ = build()
    data(owner, 101.0)
    lose_the_archive(owner, sink)
    owner.tick(*at(103.0))
    recovery = sink.groups[-1]
    for n in range(3):
        data(owner, 104.0 + n, items=(("ti", 530.0 + n),), seq=20 + n)
    assert sink.groups[-1] is recovery, "nothing may pass the recovery unit"
    owner.committed(recovery)
    waiting = [g for g in sink.groups if g.order > recovery.order]
    assert [g.order for g in waiting] == sorted(g.order for g in waiting) and len(waiting) == 3


def test_a_same_generation_recovery_inside_a_gap_carries_gap_baseline_and_batch_together():
    owner, sink, _ = build()
    data(owner, 101.0)
    lose_the_archive(owner, sink)
    owner.tick(*at(105.0))  # 3 s of silence after the last response at 102.0: expired
    assert owner.sources["ua"].state == "EXPIRED"
    groups = len(sink.groups)
    data(owner, 105.5, items=(("ti", 530.0),), seq=3)
    assert len(sink.groups) == groups + 1
    unit = sink.groups[-1]
    assert unit.kind == r.GROUP_RECOVERY and owner.sources["ua"].archive == "RECOVERING"
    kinds = [s.kind for s in unit.states]
    assert kinds[:1] == ["GAP"] and "SEG_START" in kinds and "WATERMARK" in kinds
    origins = {(o.point_id, o.origin) for o in unit.observations}
    assert ("ti", r.ORIGIN_RECEIVED) in origins and ("ti", r.ORIGIN_ASSERTION) in origins


def test_groups_behind_a_refused_one_are_voided_so_a_watermark_cannot_certify_the_gap():
    owner, _, _ = build()
    written, voided = [], []

    class Router:
        def write_group(self, group):
            if group.order == refused_order:
                return RouteResult("REFUSED", None, "WRITE_BATCH_BUDGET")
            written.append(group)
            return RouteResult("COMMITTED", "primary")

    refused_order = 0
    queue = CommitQueue(
        Router(), 100, on_committed=lambda g, res: owner.committed(g, res),
        on_refused=lambda g, why: owner.refused(g, why), is_void=owner.is_void,
    )  # fmt: skip
    owner.commit = queue.offer
    data(owner, 101.0)
    refused_order = owner.order + 1  # the next data group will be refused at the head of the queue
    data(owner, 102.0, items=(("ti", 501.0),), seq=2)
    data(owner, 103.0, items=(("ti", 502.0),), seq=3)
    for second in range(104, 116):  # watermarks queue up behind the doomed group
        keepalive(owner, float(second))
        owner.tick(*at(float(second)))
    queued = [g.order for g in queue.queue]
    assert queued and refused_order in queued

    async def settle():
        task = asyncio.create_task(queue.run())
        deadline = time.monotonic() + 5
        while queue.depth and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        await queue.stop(1.0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(settle())
    voided.extend(g for g in queued if g not in [w.order for w in written])
    assert queue.stats["refused"] == 1
    assert queue.stats["voided"] >= 1, "groups queued behind the refused one were not written"
    written_orders = [g.order for g in written]
    assert all(o < refused_order for o in written_orders), "nothing above the gap became durable"
    assert owner.sources["ua"].archive in ("LOST", "RECOVERING")


def test_against_postgres_the_recovery_unit_lands_in_one_transaction_with_every_part(archive_db_factory):
    from test_archive_router import Disks, config

    cfg = config()
    cfg_ua = type(cfg).model_validate({**cfg.model_dump(mode="json"), "sources": CONFIG.model_dump(mode="json")["sources"]})
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    router = ArchiveRouter(cfg_ua, primary, emergency, disk_usage=Disks())
    router.initialize()
    router.bind_run("run", cfg_ua.config_id, WALL0)
    sink = Sink()
    owner = Owner(cfg_ua, WALL0, 100.0, commit=sink, buckets=FixedBucket(), run_id="run")
    sink.owner = owner
    owner.begin_generation("ua", 1, WALL0, 100.0)
    data(owner, 101.0)
    lose_the_archive(owner, sink)
    owner.tick(*at(104.0))
    recovery = sink.groups[-1]
    for group in sink.groups:
        assert router.write_group(group).durable
    with primary.session() as conn:
        gid = recovery.group_id
        counts = {
            t: conn.execute(f"SELECT count(*) AS n FROM {t} WHERE group_id=%s", (gid,)).fetchone()["n"]
            for t in ("observation", "result", "state_event")
        }
        gap = conn.execute("SELECT source_id,start_wall,end_wall,reason FROM archive_gap").fetchall()
        segments = conn.execute("SELECT seg,end_reason FROM segment ORDER BY seg").fetchall()
        mark = conn.execute("SELECT seg,group_order FROM watermark ORDER BY seg DESC LIMIT 1").fetchone()
    assert counts["observation"] == 2 and counts["state_event"] == 4 and counts["result"] >= 1
    assert gap == [{"source_id": "ua", "start_wall": at(102.0)[0], "end_wall": at(104.0)[0], "reason": "QUEUE_FULL"}]
    assert segments[0]["seg"] == 1 and segments[0]["end_reason"] == "ARCHIVE_GAP" and segments[-1]["seg"] == 2
    assert mark == {"seg": 2, "group_order": recovery.order}
