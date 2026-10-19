"""R04/Q03: normal run completion is offered only after source facts settle, with one stable RUN_END identity."""

import asyncio
import threading
import time

import pytest
from owner_support import small_config
from test_archive_router import Disks
from test_capstone_d import run_worker
from test_commit_queue import ScriptedRouter
from test_source_validity import MONO0, WALL0, at, data, tick, ua_only

from htsg import records as r
from htsg.archive_router import ArchiveRouter, RouteResult
from htsg.commitq import CommitQueue
from htsg.owner import Owner


async def until(condition, why, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, why
        await asyncio.sleep(0.01)


def chain(capacity=10):
    router = ScriptedRouter()
    owner = Owner(small_config(), 1000.0, 100.0, run_id="run")
    queue = CommitQueue(
        router,
        capacity,
        on_committed=lambda group, result: owner.committed(group, result),
        on_refused=lambda group, reason: owner.refused(group, reason),
        is_void=owner.is_void,
        retry_delay=(0.01, 0.02),
    )
    owner.commit = queue.offer
    return owner, queue, router


async def finish(queue, task):
    await queue.stop(1.0)
    await asyncio.wait_for(task, 1.0)


async def test_a_full_queue_retries_the_same_run_end_group_after_room_returns():
    owner, queue, router = chain(capacity=1)
    owner.start(1000.0, 100.0)  # fills the one queue slot
    owner.shutdown(1001.0, 101.0)
    run_end = owner.run_end_group
    assert run_end is not None and owner.run_end_offered is False

    task = asyncio.create_task(queue.run())
    await until(lambda: queue.depth == 0, "RUN_START did not leave the queue")
    assert owner.submit_run_end() and owner.run_end_group is run_end
    await until(lambda: owner.run_end_status == "COMMITTED", "RUN_END was not confirmed")
    await finish(queue, task)
    assert [group_id for group_id, _ in router.written] == ["run/g1", run_end.group_id]


async def test_a_refused_run_end_never_claims_a_normal_stop():
    owner, queue, router = chain()
    owner.start(1000.0, 100.0)
    owner.shutdown(1001.0, 101.0)
    run_end = owner.run_end_group
    router.script[run_end.group_id] = ["REFUSED"]

    task = asyncio.create_task(queue.run())
    await until(lambda: owner.run_end_status == "REFUSED", "the refusal was not reported")
    await finish(queue, task)
    assert owner.run_end_state() == "REFUSED", "the end of the run is not registered, and it says why"


class HoldRecovery:
    """Real archive router whose writer thread pauses after it receives the recovery unit."""

    def __init__(self, router):
        self.router = router
        self.entered, self.release = threading.Event(), threading.Event()

    def write_group(self, group):
        if group.kind == r.GROUP_RECOVERY and any(s.kind == "GAP" for s in group.states):
            self.entered.set()
            assert self.release.wait(5), "the test did not release the recovery write"
        return self.router.write_group(group)


class RunEndOutcome:
    """Writes every dependency to the real database, but makes RUN_END resolve as scripted."""

    def __init__(self, router, status):
        self.router, self.status = router, status

    def write_group(self, group):
        if any(s.kind == "RUN_END" for s in group.states):
            return RouteResult(self.status, "primary", "scripted")
        return self.router.write_group(group)


async def test_real_database_run_end_is_committed_after_recovery_pending_data_and_segment_end(archive_db_factory):
    """R04/Q03: the database, not only an in-memory list, must show RUN_END after the recovery unit, data accepted
    behind it, and shutdown SEG_END."""
    config = ua_only()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    real = ArchiveRouter(config, primary, emergency, disk_usage=Disks())
    real.initialize("run")
    real.bind_run("run", config.config_id, WALL0)
    held = HoldRecovery(real)
    owner = Owner(config, WALL0, MONO0, run_id="run")
    queue = CommitQueue(
        held,
        20,
        on_committed=lambda group, result: owner.committed(group, result),
        on_refused=lambda group, reason: owner.refused(group, reason),
        is_void=owner.is_void,
        retry_delay=(0.01, 0.02),
    )
    owner.commit = queue.offer
    task = asyncio.create_task(queue.run())
    owner.start(WALL0, MONO0)
    owner.begin_generation("ua", 1, WALL0, MONO0)
    data(owner, 101.0)
    await until(
        lambda: owner.sources["ua"].durable == {1} and not owner.sources["ua"].inflight and queue.depth == 0,
        "the first segment and its data were not fully confirmed",
    )

    queue.capacity = 0
    data(owner, 102.0)
    queue.capacity = 20
    tick(owner, 103.0)
    assert await asyncio.to_thread(held.entered.wait, 10), "the recovery unit did not reach the writer"
    data(owner, 104.0, items=(("ti", 510.0),), seq=3)
    owner.shutdown(WALL0 + 105.0, MONO0 + 105.0)
    assert owner.run_end_group is None

    held.release.set()
    await until(lambda: owner.shutdown_progress(), "source shutdown facts did not settle", timeout=5)
    assert owner.submit_run_end()
    await until(lambda: owner.run_end_status == "COMMITTED", "RUN_END was not committed", timeout=5)
    await finish(queue, task)

    with primary.session() as conn:
        ordered = conn.execute(
            "SELECT g.order_no,e.kind FROM state_event e JOIN commit_group g USING(group_id) "
            "WHERE e.run_id='run' ORDER BY g.order_no,e.id"
        ).fetchall()
        taken = conn.execute(
            "SELECT e.kind FROM state_event e JOIN commit_group g USING(group_id) "
            "WHERE e.run_id='run' ORDER BY g.committed_at,e.id"
        ).fetchall()
        ended = conn.execute("SELECT end_reason FROM run WHERE run_id='run'").fetchone()
    assert ordered[-1]["kind"] == "RUN_END" and ordered[-1]["order_no"] == max(x["order_no"] for x in ordered)
    assert taken[-1]["kind"] == "RUN_END", "last in the order the archive took the groups, not only by the number the owner gave"
    assert ended["end_reason"] == "NORMAL"


async def test_unknown_run_end_never_marks_the_real_database_normal(archive_db_factory):
    """R04: an unresolved or explicitly refused RUN_END leaves ended_wall empty for next-start reconciliation."""
    config = small_config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    real = ArchiveRouter(config, primary, emergency, disk_usage=Disks())
    real.initialize("run")
    real.bind_run("run", config.config_id, 1000.0)
    owner = Owner(config, 1000.0, 100.0, run_id="run")
    scripted = RunEndOutcome(real, "UNKNOWN")
    queue = CommitQueue(
        scripted,
        10,
        on_committed=lambda group, result: owner.committed(group, result),
        on_refused=lambda group, reason: owner.refused(group, reason),
        is_void=owner.is_void,
        retry_delay=(0.01, 0.02),
    )
    owner.commit = queue.offer
    task = asyncio.create_task(queue.run())
    owner.start(1000.0, 100.0)
    owner.shutdown(1001.0, 101.0)
    await until(lambda: queue.stats["unknown"] >= 2, "RUN_END was not retried as UNKNOWN")
    with primary.session() as conn:
        assert conn.execute("SELECT ended_wall FROM run WHERE run_id='run'").fetchone()["ended_wall"] is None
    await queue.stop(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


class TurnAwayUnit:
    """Real archive router that holds the recovery unit at the writer and then turns it away; the final group of the stop (the
    gap and the segment ends, no new segment) is taken, or turned away as well, as told."""

    def __init__(self, router, final_verdict):
        self.router, self.final_verdict = router, final_verdict
        self.entered, self.release = threading.Event(), threading.Event()

    def write_group(self, group):
        gap = any(s.kind == "GAP" for s in group.states)
        if gap and any(s.kind == "SEG_START" for s in group.states):  # the recovery unit
            self.entered.set()
            assert self.release.wait(5), "the test did not release the recovery write"
            return RouteResult("REFUSED", "primary", "WRITE_BATCH_BUDGET")
        if gap and self.final_verdict == "REFUSE":  # the final group of the stop
            return RouteResult("REFUSED", "primary", "scripted")
        return self.router.write_group(group)


@pytest.mark.parametrize("final_verdict", ["COMMIT", "REFUSE"])
async def test_a_unit_turned_away_after_the_stop_began_is_followed_by_the_final_group_of_its_source(
    archive_db_factory, final_verdict
):
    """R04, 细则 8.1 item 2a (约定 J): a recovery unit that was on its way when the stop began is turned away.  The unit is large
    (the whole retained state); what the stop needs is small - the gap up to the stop and the end of the segments the archive
    holds open - so the source gets that one more try.  Taken: the gap is stored, the segment is closed where the loss began, and
    the end of the run is registered (last, by commit time).  Turned away as well: nothing is registered, and the next start
    records an ABNORMAL_EXIT."""
    config = ua_only()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    real = ArchiveRouter(config, primary, emergency, disk_usage=Disks())
    real.initialize("run")
    real.bind_run("run", config.config_id, WALL0)
    held = TurnAwayUnit(real, final_verdict)
    owner = Owner(config, WALL0, MONO0, run_id="run")
    queue = CommitQueue(
        held,
        20,
        on_committed=lambda group, result: owner.committed(group, result),
        on_refused=lambda group, reason: owner.refused(group, reason),
        is_void=owner.is_void,
        retry_delay=(0.01, 0.02),
    )
    owner.commit = queue.offer
    task = asyncio.create_task(queue.run())
    owner.start(WALL0, MONO0)
    owner.begin_generation("ua", 1, WALL0, MONO0)
    data(owner, 101.0)
    s = owner.sources["ua"]
    await until(lambda: s.durable == {1} and not s.inflight and queue.depth == 0, "segment 1 and its data were not confirmed")

    queue.capacity = 0
    data(owner, 102.0, items=(("ti", 501.0),), seq=2)  # turned away: the archive is lost from here
    queue.capacity = 20
    assert s.archive == "LOST"
    tick(owner, 103.0)  # the recovery unit is built and reaches the writer
    assert await asyncio.to_thread(held.entered.wait, 10), "the recovery unit did not reach the writer"
    data(owner, 104.0, items=(("ti", 510.0),), seq=3)  # waits behind the unit
    owner.shutdown(*at(105.0))
    assert owner.run_end_group is None and s.archive == "RECOVERING", "the stop began with the unit on its way"

    held.release.set()  # ... and now the archive turns the unit away
    await until(lambda: owner.shutdown_progress() or s.shutdown_failed, "the stop neither completed nor failed", timeout=5)
    if final_verdict == "COMMIT":
        assert owner.submit_run_end()
        await until(lambda: owner.run_end_status == "COMMITTED", "the end of the run was not confirmed", timeout=5)
    else:
        assert s.shutdown_failed and owner.submit_run_end() is False and owner.run_end_group is None
    await finish(queue, task)

    with primary.session() as conn:
        run = conn.execute("SELECT ended_wall,end_reason FROM run WHERE run_id='run'").fetchone()
        segments = conn.execute("SELECT seg,end_wall,end_reason FROM segment ORDER BY seg").fetchall()
        gaps = conn.execute("SELECT start_wall,end_wall,reason FROM archive_gap").fetchall()
        run_ends = conn.execute("SELECT count(*) AS n FROM state_event WHERE kind='RUN_END'").fetchone()["n"]
        last = conn.execute(
            "SELECT e.kind FROM state_event e JOIN commit_group g USING (group_id) ORDER BY g.committed_at DESC,e.id DESC LIMIT 1"
        ).fetchone()
    if final_verdict == "COMMIT":
        assert (run["end_reason"], run["ended_wall"]) == ("NORMAL", at(105.0)[0])
        assert [(x["seg"], x["end_wall"], x["end_reason"]) for x in segments] == [(1, at(102.0)[0], "ARCHIVE_GAP")], (
            "segment 1 ends where the loss began; segment 2 only ever existed in the unit that was turned away"
        )
        assert [(x["start_wall"], x["end_wall"], x["reason"]) for x in gaps] == [(at(102.0)[0], at(105.0)[0], "QUEUE_FULL")]
        assert run_ends == 1 and last["kind"] == "RUN_END", "the end of the run is the last thing the archive took"
    else:
        assert run["ended_wall"] is None and run_ends == 0
        assert [(x["seg"], x["end_wall"]) for x in segments] == [(1, None)] and gaps == []
        ArchiveRouter(config, primary, emergency, disk_usage=Disks()).initialize("next-run")  # the next start
        with primary.session() as conn:
            assert conn.execute("SELECT end_reason FROM run WHERE run_id='run'").fetchone()["end_reason"] == "ABNORMAL_EXIT"
            assert conn.execute("SELECT end_reason FROM segment").fetchone()["end_reason"] == "EXIT_TRUNCATED"


@pytest.mark.parametrize(("mode", "ended"), [("before_end", False), ("after_end", True)])
def test_a_process_killed_before_the_end_of_the_run_is_written_is_an_abnormal_exit_not_a_normal_one(
    archive_db_factory, mode, ended
):
    """R04/Q03 (细则 8.2-1): the stop wrote everything else - the segments are closed - and the process died just before the
    end of the run was written (or just after it, before it learned so).  Only the end itself decides what the next start says."""
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    dsns = {"primary": primary.dsn, "emergency": emergency.dsn}
    first = run_worker(mode, "run1", 0, dsns)
    assert first.returncode == 9, (first.returncode, first.stderr[-600:])
    with primary.session() as conn:
        run = conn.execute("SELECT ended_wall,end_reason FROM run WHERE run_id='run1'").fetchone()
        segments = conn.execute("SELECT end_wall,end_reason FROM segment WHERE run_id='run1'").fetchall()
    assert (run["end_reason"] == "NORMAL") is ended and (run["ended_wall"] is not None) is ended
    assert segments and all(x["end_reason"] == "SHUTDOWN" and x["end_wall"] is not None for x in segments), "the stop closed them"

    second = run_worker("none", "run2", 0, dsns, wall=2000)  # the next start
    assert second.returncode == 0, second.stderr[-600:]
    with primary.session() as conn:
        old = conn.execute("SELECT end_reason FROM run WHERE run_id='run1'").fetchone()
        after = conn.execute("SELECT end_reason FROM segment WHERE run_id='run1'").fetchall()
    assert old["end_reason"] == ("NORMAL" if ended else "ABNORMAL_EXIT"), "a normal end stays one; no end becomes an abnormal exit"
    assert [x["end_reason"] for x in after] == ["SHUTDOWN"] * len(segments), "segments the stop closed are not cut again"
