"""R04 / Q03 at the level of the real runtime: the end of a run is registered from what the archive CONFIRMED (细则 8.2).

Why it matters: a run recorded as ended normally tells history that every segment was closed and nothing is owed.  The stop used to
register that at once - behind a recovery unit, ahead of the data the unit was still holding back - and a source whose archive was
lost never had its gap written.  These tests run the stop itself (`Runtime.stop`: its single deadline, its early exit when a source
has failed or is settled, the end offered again while the queue is full) with the real runtime, queue, owner and router over two
throw-away databases.  `Wire` stands between the writer thread and the router: it notes what was written and in which order, and
can hold a write back, turn it away, or answer UNKNOWN.  What the databases hold is read back with SQL - the run, the segments, the
gaps, and the order in which the archive TOOK the groups (commit time, not the number the owner gave a group).
"""

import asyncio
import tempfile
import threading
import time
from collections import namedtuple
from pathlib import Path

import pytest
from owner_support import notify, poll, small_config
from test_archive_router import Disks
from test_runtime import Field
from test_runtime import build as build_full_scale
from test_runtime import until as wait_until
from test_source_validity import ua_only

from htsg import records as r
from htsg import runtime as runtime_module
from htsg.archive_router import ArchiveRouter, RouteResult
from htsg.runtime import Runtime

Wrote = namedtuple("Wrote", "group_id source kinds data status")


class Rule:
    def __init__(self, match, hold, verdict, times):
        self.match, self.verdict, self.times, self.seen = match, verdict, times, 0
        self.gate = threading.Event() if hold else None

    def release(self):
        if self.gate is not None:
            self.gate.set()


class Wire:
    """Between the writer thread and the router: notes what it was asked to write and how that ended, in order.  A rule holds a
    write back until released, turns it away (REFUSED), or answers UNKNOWN - with the group written (the commit happened and the
    writer was not told) or not.  `delay` makes every write slow."""

    def __init__(self, router):
        self.write = router.write_group
        router.write_group = self
        self.log, self.rules, self.delay = [], [], 0.0

    def rule(self, match, *, hold=False, verdict=None, times=None):
        rule = Rule(match, hold, verdict, times)
        self.rules.append(rule)
        return rule

    def __call__(self, group):
        if self.delay:
            time.sleep(self.delay)
        rule = next((x for x in self.rules if x.match(group) and (x.times is None or x.seen < x.times)), None)
        if rule is None:
            result = self.write(group)
        else:
            rule.seen += 1
            if rule.gate is not None:
                rule.gate.wait(30)
            if rule.verdict == "REFUSED":
                result = RouteResult("REFUSED", None, "TEST")
            elif rule.verdict == "UNKNOWN":
                result = RouteResult("UNKNOWN", "primary", "TEST")
            else:
                result = self.write(group)
                if rule.verdict == "UNKNOWN_WROTE":
                    result = RouteResult("UNKNOWN", result.target, "TEST")
        kinds = tuple(s.kind for s in group.states)
        self.log.append(Wrote(group.group_id, group.source_id, kinds, bool(group.observations), result.status))
        return result

    def written(self):
        return [x for x in self.log if x.status in ("COMMITTED", "ALREADY")]


def carries(kind):
    return lambda group: any(s.kind == kind for s in group.states)


def is_end(group):
    return carries("RUN_END")(group)


def is_unit(group):
    """The recovery unit: the gap AND a new segment with the retained state."""
    return group.kind == r.GROUP_RECOVERY and carries("GAP")(group) and carries("SEG_START")(group)


def is_final(group):
    """The final group of a source at the stop: the gap and the segment ends, no new segment."""
    return carries("GAP")(group) and not carries("SEG_START")(group)


def has_data(group):
    return group.source_id == "ua" and bool(group.observations)


def has_data_of(source):
    return lambda group: group.source_id == source and bool(group.observations)


def is_final_of(source):
    return lambda group: group.source_id == source and is_final(group)


def is_end_entry(entry):
    return "RUN_END" in entry.kinds


def quiet_config(commit_queue=None):
    """The OPC UA source alone, pointed where nothing listens: the tests feed the owner by hand."""
    payload = ua_only().model_dump(mode="json")
    payload["sources"][1]["endpoint"] = "opc.tcp://127.0.0.1:1/"
    if commit_queue is not None:
        payload["limits"] = {"commit_queue": commit_queue}
    return type(ua_only()).model_validate(payload)


def two_sources(first):
    """Both the Modbus and the OPC UA source switched on, each pointed where nothing listens and fed by hand; `first` is listed
    first, so it is the source whose final group the owner offers first at the stop."""
    payload = small_config().model_dump(mode="json")
    payload["sources"][0]["port"] = 1
    payload["sources"][1]["endpoint"] = "opc.tcp://127.0.0.1:1/"
    payload["sources"].sort(key=lambda source: source["id"] != first)
    return type(small_config()).model_validate(payload)


class Lab:
    def __init__(self, factory, monkeypatch, *, wait=None, commit_queue=None, disks=None, config=None):
        if wait is not None:
            monkeypatch.setattr(runtime_module, "STOP_WRITE_WAIT", wait)
        self.primary, self.emergency = factory("primary"), factory("emergency")
        self.config = config or quiet_config(commit_queue)
        self.disks = disks or Disks()
        self.router = ArchiveRouter(self.config, self.primary, self.emergency, disk_usage=self.disks)
        self.wire = Wire(self.router)
        self.numbers = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)  # this lab's own bucket number file
        self.runtime = Runtime(self.config, self.router, number_file=Path(self.numbers.name) / "bucket_number.json")
        self.owner, self.queue = self.runtime.owner, self.runtime.queue
        self.seq, self.stopped, self.took = 0, False, None

    @property
    def ua(self):
        return self.owner.sources["ua"]

    async def __aenter__(self):
        await self.runtime.start()
        for source in self.config.sources:
            if source.enabled:
                self.owner.begin_generation(source.id, 1, time.time(), time.monotonic())
        return self

    async def __aexit__(self, *exc):
        for rule in self.wire.rules:
            rule.release()
        if not self.stopped:
            await self.stop()

    async def stop(self):
        started = time.monotonic()
        await self.runtime.stop()
        self.took, self.stopped = time.monotonic() - started, True

    def feed(self):
        self.seq += 1
        wall, mono = time.time(), time.monotonic()
        assert self.owner.accept(notify([("ti", 500.0 + self.seq), ("to", 450.0)], wall=wall, mono=mono, seq=self.seq))

    def feed_mb(self):
        self.seq += 1
        wall, mono = time.time(), time.monotonic()
        assert self.owner.accept(poll(self.seq, {"b1": 0.1 + self.seq / 1000, "b2": 0.2}, wall=wall, mono=mono))

    def tick(self):
        self.owner.tick(time.time(), time.monotonic())

    async def until(self, condition, why, seconds=5.0):
        deadline = time.monotonic() + seconds
        while not condition():
            assert time.monotonic() < deadline, f"timed out: {why}"
            await asyncio.sleep(0.01)

    async def stored(self, *sources):
        """Segment 1 and the first data of the sources (default: the OPC UA one) are in the archive, and nothing of them is on its way."""
        owned = [self.owner.sources[x] for x in sources or ("ua",)]
        await self.until(lambda: all(1 in s.durable and not s.inflight for s in owned), "segment 1 and its data are confirmed")

    async def drained(self):
        await self.stored()
        await self.until(lambda: self.queue.depth == 0, "the queue is empty")

    async def lose(self, match=None):
        """The writer turns away the next group of the source (the default: data): the archive is lost from its moment."""
        self.wire.rule(match or has_data, verdict="REFUSED", times=1)
        self.feed()
        await self.until(lambda: self.ua.archive == "LOST" and not self.ua.inflight, "the loss is noticed")


def rows(store, query, *args):
    with store.session() as conn:
        return conn.execute(query, args).fetchall()


def run_of(store):
    (run,) = rows(store, "SELECT ended_wall,end_reason FROM run")
    return run["ended_wall"], run["end_reason"]


def segments_of(store):
    return rows(store, "SELECT source_id,seg,start_wall,end_wall,end_reason FROM segment ORDER BY source_id,seg")


def gaps_of(store):
    return rows(store, "SELECT start_wall,end_wall,reason FROM archive_gap ORDER BY start_wall")


def count(store, table, where="true"):
    return rows(store, f"SELECT count(*) AS n FROM {table} WHERE {where}")[0]["n"]


def taken_in_order(store):
    """The kinds of state records in the order the archive took their groups."""
    query = "SELECT e.kind FROM state_event e JOIN commit_group g USING (group_id) ORDER BY g.committed_at,e.id"
    return [x["kind"] for x in rows(store, query)]


def not_whole(store):
    """The stored groups that do not hold all the records they say they hold."""
    members = " + ".join(
        f"(SELECT count(*) FROM {table} m WHERE m.group_id=c.group_id)"
        for table in ("observation", "result", "state_event", "fault_record")
    )
    return [x["group_id"] for x in rows(store, f"SELECT c.group_id FROM commit_group c WHERE c.records <> {members}")]


def stop_facts(runtime):
    """What the stop reports: (shutdown_incomplete, run_end_status, failed_sources, unsettled_queue_items)."""
    h = runtime.health
    return h["shutdown_incomplete"], h["run_end_status"], h["failed_sources"], h["unsettled_queue_items"]


def restart(lab):
    """The next start of the process: a new run in the same databases closes what the old one left open."""
    router = ArchiveRouter(lab.config, lab.primary, lab.emergency, disk_usage=Disks())
    router.initialize("the-next-run")
    return router


# ====================================================================================== the stop that goes well


async def test_a_normal_stop_registers_the_end_last_by_commit_time_and_takes_a_fraction_of_the_deadline(
    archive_db_factory, monkeypatch
):
    lab = Lab(archive_db_factory, monkeypatch)
    async with lab:
        lab.feed()
        await lab.stored()
        await lab.stop()
        assert is_end_entry(lab.wire.written()[-1]) and sum(is_end_entry(x) for x in lab.wire.written()) == 1
        assert taken_in_order(lab.primary)[-1] == "RUN_END"
        assert run_of(lab.primary)[1] == "NORMAL" and run_of(lab.primary)[0] is not None
        assert all(x["end_wall"] is not None for x in segments_of(lab.primary)), "no segment is left open"
        assert stop_facts(lab.runtime) == (False, "COMMITTED", [], 0)
    assert lab.took < 3.0, f"a stop with nothing in the way took {lab.took:.2f} s of the 8 s it may take"
    print(f"\nnormal stop, one source: {lab.took:.3f} s")


async def test_a_normal_stop_at_full_scale_takes_a_fraction_of_the_deadline(archive_db_factory, tmp_path):
    """细则 8.2-5: 8 Modbus bridges, 200 OPC UA items, real servers, real databases; the measured value is printed."""
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    async with Field() as field:
        runtime, _, _ = build_full_scale(field, tmp_path, primary, emergency)
        await runtime.start()
        try:
            await wait_until(lambda: all(v["valid"] for v in runtime.owner.channel_views().values()), "all channels valid", 60)
            await wait_until(lambda: runtime.queue.depth == 0 and runtime.queue.stats["committed"] > 3, "the archive has written", 60)
            await asyncio.sleep(3)
        finally:
            started = time.monotonic()
            await runtime.stop()
            took = time.monotonic() - started
    assert took < 9.0, f"the stop took {took:.2f} s: the deadline is 8 s plus 1 s"
    assert stop_facts(runtime) == (False, "COMMITTED", [], 0) and count(primary, "state_event", "kind='RUN_END'") == 1
    assert rows(primary, "SELECT end_reason FROM run")[0]["end_reason"] == "NORMAL"
    assert count(primary, "segment", "end_wall IS NULL") == 0, "no segment is left open"
    print(f"\nnormal stop at full scale: {took:.3f} s")


async def test_the_end_is_the_last_group_the_archive_takes_when_a_recovery_unit_is_confirmed_late(
    archive_db_factory, monkeypatch
):
    """Q03: the stop came while the unit was still on its way; the data and the segment end held back behind it used to be
    written AFTER the end of the run.  Asserted on the writer's own log and on the commit times in the database."""
    lab = Lab(archive_db_factory, monkeypatch)
    async with lab:
        lab.feed()
        await lab.stored()
        await lab.lose()
        unit = lab.wire.rule(is_unit, hold=True)
        lab.tick()  # the owner builds the unit; the writer holds it
        assert lab.ua.archive == "RECOVERING"
        lab.feed()
        lab.feed()  # both wait behind the unit
        stopping = asyncio.create_task(lab.stop())
        await asyncio.sleep(0.3)
        assert not stopping.done(), "the stop waits for the unit instead of registering the end ahead of it"
        unit.release()
        await asyncio.wait_for(stopping, 10)
        written = lab.wire.written()
        assert is_end_entry(written[-1]) and sum(is_end_entry(x) for x in written) == 1
        unit_at = next(i for i, x in enumerate(written) if "GAP" in x.kinds)
        behind = [i for i, x in enumerate(written) if x.source == "ua" and x.data and i > unit_at]
        assert len(behind) == 2 and "SEG_END" in written[-2].kinds, "the data and the segment end followed the unit, then the end"
        assert taken_in_order(lab.primary)[-1] == "RUN_END" and taken_in_order(lab.primary).count("RUN_END") == 1
        assert run_of(lab.primary)[1] == "NORMAL"
        assert all(x["end_wall"] is not None for x in segments_of(lab.primary)), "every segment is closed in the archive"
        assert len(gaps_of(lab.primary)) == 1 and not not_whole(lab.primary)
        assert stop_facts(lab.runtime) == (False, "COMMITTED", [], 0)


@pytest.mark.parametrize("how", ["VALID", "RESYNC", "EXPIRED"])
async def test_a_source_whose_archive_is_lost_gets_its_gap_and_its_segment_end_at_the_stop(archive_db_factory, monkeypatch, how):
    """Q03 / 补充 A: LOST with the source in each of its three states.  The segment is closed where the loss began, the gap runs
    up to the stop, and no baseline of the retained state is written.  The segment says why it ended as a recovery unit would
    have said it: one left before the loss keeps the reason it was left with, one the loss cut off says ARCHIVE_GAP."""
    lab = Lab(archive_db_factory, monkeypatch)
    async with lab:
        lab.feed()
        await lab.stored()
        if how == "VALID":
            monkeypatch.setattr(lab.owner, "_try_archive_recovery", lambda *args: None)  # the retry at the tick is held off
            await lab.lose()
        elif how == "RESYNC":
            lab.wire.rule(carries("SEG_END"), verdict="REFUSED", times=1)
            lab.owner.source_down("ua", 1, "CONNECTION", time.time(), time.monotonic())
            await lab.until(lambda: lab.ua.archive == "LOST" and not lab.ua.inflight, "the end of the segment is turned away")
        else:
            lab.wire.rule(carries("SEG_END"), verdict="REFUSED", times=1)
            await lab.until(lambda: lab.ua.archive == "LOST", "the expiry's end of the segment is turned away", seconds=8)
        assert lab.ua.state == how
        lost_since, left = lab.ua.lost_since_wall, dict(lab.ua.closing)
        stopped_at = time.time()
        await lab.stop()
        (segment,) = segments_of(lab.primary)
        left_at = lost_since if how == "VALID" else min(left[1][1], lost_since)  # where the segment was left, if before the loss
        assert segment["end_wall"] == left_at, "the segment ends where it was left, or where the loss began if that came first"
        if how == "VALID":
            assert segment["end_reason"] == "ARCHIVE_GAP", "the loss ended it - the stop is told by the end of the run"
        else:
            assert segment["end_reason"] == left[1][0], "a segment left at the moment of the loss keeps the reason it was left with"
        (gap,) = gaps_of(lab.primary)
        assert gap["start_wall"] == lost_since and stopped_at <= gap["end_wall"] <= time.time()
        assert gap["reason"].startswith("REFUSED")
        assert count(lab.primary, "observation", "origin = 'STATE_ASSERTION'") == 0, "no baseline of the retained state"
        assert count(lab.primary, "result", "origin = 'STATE_ASSERTION'") == 0 and count(lab.primary, "watermark") == 0
        assert run_of(lab.primary)[1] == "NORMAL" and taken_in_order(lab.primary)[-1] == "RUN_END"
        assert stop_facts(lab.runtime) == (False, "COMMITTED", [], 0) and lab.took < 3.0


async def test_a_source_that_left_its_segment_after_the_loss_began_has_it_ended_by_the_loss(archive_db_factory, monkeypatch):
    """The segment ends where the loss began, whatever the source did later: it was the loss that ended what the archive
    holds, not the reconnect that came after it, so the reason is ARCHIVE_GAP and not the one the source left with."""
    lab = Lab(archive_db_factory, monkeypatch)
    async with lab:
        lab.feed()
        await lab.stored()
        monkeypatch.setattr(lab.owner, "_try_archive_recovery", lambda *args: None)  # the retry at the tick is held off
        await lab.lose()
        await asyncio.sleep(0.05)
        lab.owner.source_down("ua", 1, "CONNECTION", time.time(), time.monotonic())  # left AFTER the loss began
        lost_since, (left_reason, left_end) = lab.ua.lost_since_wall, lab.ua.closing[1]
        assert lab.ua.state == "RESYNC" and left_end > lost_since and left_reason != "ARCHIVE_GAP"
        await lab.stop()
        (segment,) = segments_of(lab.primary)
        assert (segment["end_wall"], segment["end_reason"]) == (lost_since, "ARCHIVE_GAP")
        assert run_of(lab.primary)[1] == "NORMAL" and stop_facts(lab.runtime) == (False, "COMMITTED", [], 0)


# ================================================================================ the stop that cannot be completed


async def test_a_full_queue_at_the_stop_with_a_stuck_writer_registers_no_end_and_the_next_start_closes_the_run(
    archive_db_factory, monkeypatch
):
    lab = Lab(archive_db_factory, monkeypatch, wait=0.5, commit_queue=2)
    async with lab:
        await lab.drained()  # the start of the run and of segment 1 are in: a queue of two has room again
        lab.feed()
        await lab.drained()
        lab.wire.rule(lambda group: True, hold=True)  # the writer is stuck: the queue fills up
        lab.feed()
        lab.feed()
        assert lab.queue.depth == 2 and lab.ua.archive == "OK", "full, and nothing lost yet"
        await lab.stop()
        assert lab.ua.archive == "LOST" and lab.ua.lost_reason == "QUEUE_FULL", "the end of the segment found no room"
        incomplete, end, failed, stuck = stop_facts(lab.runtime)
        assert (incomplete, end, failed) == (True, "NOT_QUEUED", []) and stuck >= 1 and 0.4 < lab.took < 1.0
        assert run_of(lab.primary) == (None, None) and count(lab.primary, "state_event", "kind='RUN_END'") == 0
        assert [(x["seg"], x["end_wall"]) for x in segments_of(lab.primary)] == [(1, None)] and gaps_of(lab.primary) == []
        restart(lab)
        assert run_of(lab.primary)[1] == "ABNORMAL_EXIT"
        assert [x["end_reason"] for x in segments_of(lab.primary)] == ["EXIT_TRUNCATED"]


async def test_a_full_queue_at_the_stop_with_a_slow_writer_still_gets_the_final_group_in(archive_db_factory, monkeypatch):
    """The queue is full when the stop comes, but the writer is only slow: the final group of the source is offered again until
    there is room, and the stop ends normally with the gap that began when the queue turned the data away."""
    lab = Lab(archive_db_factory, monkeypatch, commit_queue=2)
    async with lab:
        await lab.drained()  # the start of the run and of segment 1 are in: a queue of two has room again
        lab.feed()
        await lab.drained()
        lab.wire.delay = 0.3
        for _ in range(3):  # one is being written, one waits, the third finds no room
            lab.feed()
        assert lab.ua.archive == "LOST" and lab.ua.lost_reason == "QUEUE_FULL"
        await lab.stop()
        assert run_of(lab.primary)[1] == "NORMAL" and taken_in_order(lab.primary)[-1] == "RUN_END"
        (gap,) = gaps_of(lab.primary)
        assert gap["reason"] == "QUEUE_FULL"
        (segment,) = segments_of(lab.primary)
        assert segment["end_wall"] == gap["start_wall"], "closed where the loss began"
        assert stop_facts(lab.runtime) == (False, "COMMITTED", [], 0) and lab.took < 8.0


async def test_the_stop_ends_at_the_deadline_when_the_archive_does_not_answer(archive_db_factory, monkeypatch):
    """The deadline is for everything - settling the sources and the end - and the writer is not given a second wait."""
    lab = Lab(archive_db_factory, monkeypatch, wait=0.5)
    async with lab:
        lab.feed()
        await lab.stored()
        lab.wire.rule(lambda group: True, hold=True)
        lab.feed()
        await lab.stop()
        incomplete, end, failed, stuck = stop_facts(lab.runtime)
        assert (incomplete, end, failed) == (True, "NOT_QUEUED", []) and stuck >= 1 and 0.4 < lab.took < 1.0, lab.took
        assert run_of(lab.primary) == (None, None) and count(lab.primary, "state_event", "kind='RUN_END'") == 0
        restart(lab)
        assert run_of(lab.primary)[1] == "ABNORMAL_EXIT"


async def test_the_stop_given_the_real_eight_seconds_ends_within_nine(archive_db_factory, monkeypatch):
    """细则 8.2-5: no monkeypatched deadline - the constant the runtime really has."""
    assert runtime_module.STOP_WRITE_WAIT == 8.0
    lab = Lab(archive_db_factory, monkeypatch)
    async with lab:
        lab.feed()
        await lab.stored()
        lab.wire.rule(lambda group: True, hold=True)
        lab.feed()
        await lab.stop()
        assert run_of(lab.primary) == (None, None) and stop_facts(lab.runtime)[:3] == (True, "NOT_QUEUED", [])
    assert 7.9 <= lab.took < 9.0, f"the stop took {lab.took:.2f} s: the deadline is 8 s plus 1 s"
    print(f"\nstop at the real deadline: {lab.took:.3f} s")


async def test_an_end_whose_commit_stays_unknown_is_not_registered_and_the_stop_ends_at_the_deadline(
    archive_db_factory, monkeypatch
):
    lab = Lab(archive_db_factory, monkeypatch, wait=0.5)
    async with lab:
        lab.feed()
        await lab.stored()
        lab.wire.rule(is_end, verdict="UNKNOWN")
        await lab.stop()
        incomplete, end, failed, stuck = stop_facts(lab.runtime)
        assert (incomplete, end, failed) == (True, "UNCONFIRMED", []) and stuck == 1 and 0.4 < lab.took < 1.0, lab.took
        assert run_of(lab.primary) == (None, None) and count(lab.primary, "state_event", "kind='RUN_END'") == 0
        assert [x["end_reason"] for x in segments_of(lab.primary)] == ["SHUTDOWN"], "the segment was closed before the end"
        restart(lab)
        assert run_of(lab.primary)[1] == "ABNORMAL_EXIT"
        assert [x["end_reason"] for x in segments_of(lab.primary)] == ["SHUTDOWN"], "and is not cut again"


async def test_a_unit_and_the_final_group_both_turned_away_end_the_stop_at_once_without_the_end(archive_db_factory, monkeypatch):
    """A source that cannot be settled ends the wait: the stop does not sit out the 8 s for something that cannot happen."""
    lab = Lab(archive_db_factory, monkeypatch)
    async with lab:
        lab.feed()
        await lab.stored()
        await lab.lose()
        unit = lab.wire.rule(is_unit, hold=True, verdict="REFUSED", times=1)
        lab.wire.rule(is_final, verdict="REFUSED")
        lab.tick()
        assert lab.ua.archive == "RECOVERING"
        lab.feed()
        stopping = asyncio.create_task(lab.stop())
        await asyncio.sleep(0.2)
        unit.release()
        await asyncio.wait_for(stopping, 10)
        assert lab.took < 3.0, f"the stop took {lab.took:.2f} s although nothing could be settled any more"
        assert stop_facts(lab.runtime)[:3] == (True, "NOT_QUEUED", ["ua"]), "the source that could not be settled is named"
        assert run_of(lab.primary) == (None, None) and count(lab.primary, "state_event", "kind='RUN_END'") == 0
        restart(lab)
        assert run_of(lab.primary)[1] == "ABNORMAL_EXIT"
        assert [x["end_reason"] for x in segments_of(lab.primary)] == ["EXIT_TRUNCATED"]


@pytest.mark.parametrize("failed", ["mb", "ua"])
async def test_a_source_whose_final_group_is_turned_away_does_not_take_the_other_source_down_with_it(
    archive_db_factory, monkeypatch, failed
):
    """R04: one source that cannot be settled keeps the run from being registered as ended, but the others still get their
    gap and their segment ends into the archive - the stop goes on until every source is done or has failed.  Both orders:
    the source that fails is the first one the owner offers a final group for, and then the second."""
    good = "ua" if failed == "mb" else "mb"
    lab = Lab(archive_db_factory, monkeypatch, config=two_sources(first=failed))
    async with lab:
        lab.feed()
        lab.feed_mb()
        await lab.stored("ua", "mb")
        monkeypatch.setattr(lab.owner, "_try_archive_recovery", lambda *args: None)  # the retry at the tick is held off
        for source, feed in (("ua", lab.feed), ("mb", lab.feed_mb)):
            lab.wire.rule(has_data_of(source), verdict="REFUSED", times=1)
            feed()
        await lab.until(
            lambda: all(lab.owner.sources[x].archive == "LOST" and not lab.owner.sources[x].inflight for x in ("ua", "mb")),
            "both archives are lost",
        )
        lab.wire.rule(is_final_of(failed), verdict="REFUSED")
        later = lab.wire.rule(is_final_of(good), hold=True)
        stopping = asyncio.create_task(lab.stop())
        await lab.until(lambda: lab.owner.sources[failed].shutdown_failed, "the first final group is turned away")
        await asyncio.sleep(0.3)
        assert not stopping.done(), "the stop does not walk away from the source that is still being settled"
        later.release()
        await asyncio.wait_for(stopping, 10)
        assert stop_facts(lab.runtime) == (True, "NOT_QUEUED", [failed], 0)
        assert run_of(lab.primary) == (None, None) and count(lab.primary, "state_event", "kind='RUN_END'") == 0
        segments = {x["source_id"]: x for x in segments_of(lab.primary)}
        (gap,) = rows(lab.primary, "SELECT source_id,start_wall,end_wall,reason FROM archive_gap")
        assert gap["source_id"] == good and gap["reason"].startswith("REFUSED"), "the gap of the source that could be settled is stored"
        assert (segments[good]["end_wall"], segments[good]["end_reason"]) == (gap["start_wall"], "ARCHIVE_GAP")
        assert segments[failed]["end_wall"] is None, "the other one was turned away: its segment is left for the next start to close"
        assert not not_whole(lab.primary)
        restart(lab)
        assert run_of(lab.primary)[1] == "ABNORMAL_EXIT"
        segments = {x["source_id"]: x for x in segments_of(lab.primary)}
        assert (segments[good]["end_reason"], segments[failed]["end_reason"]) == ("ARCHIVE_GAP", "EXIT_TRUNCATED")


@pytest.mark.parametrize("failed", ["mb", "ua"])
async def test_a_source_still_recovering_when_another_is_turned_away_is_settled_afterwards_and_none_is_left_half_settled(
    archive_db_factory, monkeypatch, failed
):
    """R04, "one source is turned away first, another completes its recovery later": the unit of the second source is still on its
    way when the stop begins and the final group of the first is turned away behind it; the second source's recovery is confirmed
    and its segment end follows.  The stop goes on advancing the sources until each is done or has failed: it returns with every
    source settled, and with the gap and the segment end of the second one stored."""
    good = "ua" if failed == "mb" else "mb"
    lab = Lab(archive_db_factory, monkeypatch, config=two_sources(first=failed))
    async with lab:
        lab.feed()
        lab.feed_mb()
        await lab.stored("ua", "mb")
        recover = lab.owner._try_archive_recovery
        monkeypatch.setattr(lab.owner, "_try_archive_recovery", lambda s, wall, mono: recover(s, wall, mono) if s.id == good else None)
        for source, feed in (("ua", lab.feed), ("mb", lab.feed_mb)):
            lab.wire.rule(has_data_of(source), verdict="REFUSED", times=1)
            feed()
        await lab.until(
            lambda: all(lab.owner.sources[x].archive == "LOST" and not lab.owner.sources[x].inflight for x in ("ua", "mb")),
            "both archives are lost",
        )
        unit = lab.wire.rule(lambda group: group.source_id == good and is_unit(group), hold=True)
        lab.wire.rule(is_final_of(failed), verdict="REFUSED")
        end = lab.wire.rule(lambda group: group.source_id == good and carries("SEG_END")(group) and not is_unit(group), hold=True)
        lab.tick()  # only the source that is to recover gets its unit, and the writer holds it
        assert lab.owner.sources[good].archive == "RECOVERING"
        stopping = asyncio.create_task(lab.stop())
        await lab.until(lambda: lab.queue.depth == 2, "the final group of the other source waits behind the unit")
        unit.release()
        await lab.until(lambda: lab.owner.sources[failed].shutdown_failed, "the other source is turned away")
        await asyncio.sleep(0.3)
        assert not stopping.done(), "the segment end of the recovering source is still to come"
        end.release()
        await asyncio.wait_for(stopping, 10)
        assert lab.owner.shutdown_settled() and lab.owner.sources[good].shutdown_done, "no source is left half settled"
        assert stop_facts(lab.runtime) == (True, "NOT_QUEUED", [failed], 0)
        assert run_of(lab.primary) == (None, None) and count(lab.primary, "state_event", "kind='RUN_END'") == 0
        (gap,) = rows(lab.primary, "SELECT source_id FROM archive_gap")
        assert gap["source_id"] == good, "the recovering source has its gap"
        stored = segments_of(lab.primary)
        assert all(x["end_wall"] is not None for x in stored if x["source_id"] == good), "and every one of its segments is closed"
        assert [x["end_wall"] for x in stored if x["source_id"] == failed] == [None], "the one turned away is left for the next start"
        assert not not_whole(lab.primary)


# ======================================================================================= the end, offered again


async def test_an_end_that_was_committed_but_reported_unknown_is_registered_once_and_whole(archive_db_factory, monkeypatch):
    lab = Lab(archive_db_factory, monkeypatch)
    async with lab:
        lab.feed()
        await lab.stored()
        lab.wire.rule(is_end, verdict="UNKNOWN_WROTE", times=1)
        await lab.stop()
        assert [x.status for x in lab.wire.log if is_end_entry(x)] == ["UNKNOWN", "ALREADY"], "the same group, asked again"
        assert count(lab.primary, "state_event", "kind='RUN_END'") == 1 and not not_whole(lab.primary)
        assert run_of(lab.primary)[1] == "NORMAL"


async def test_a_full_queue_is_asked_again_for_the_end_with_the_same_group(archive_db_factory, monkeypatch):
    lab = Lab(archive_db_factory, monkeypatch)
    offered, real = [], lab.owner.commit

    def commit(group):
        if is_end(group):
            offered.append(group)
            if len(offered) <= 3:
                return False  # no room
        return real(group)

    lab.owner.commit = commit
    async with lab:
        lab.feed()
        await lab.stored()
        await lab.stop()
        assert len(offered) == 4 and all(group is offered[0] for group in offered), "one group, offered until the queue took it"
        assert count(lab.primary, "state_event", "kind='RUN_END'") == 1 and not not_whole(lab.primary)
        assert run_of(lab.primary)[1] == "NORMAL" and lab.owner.health["run_group_rejected"] == 3


async def test_a_start_of_run_the_queue_did_not_take_leaves_the_run_to_be_created_by_the_first_group_after_it(
    archive_db_factory, monkeypatch
):
    """Not a new mechanism (细则 8.1-9): the run row is written inside whichever group of the run comes first."""
    lab = Lab(archive_db_factory, monkeypatch)
    real, calls = lab.owner.commit, []

    def commit(group):
        calls.append(group)
        return False if len(calls) == 1 else real(group)  # only the very first offer, the start of the run, is turned away

    lab.owner.commit = commit
    async with lab:
        assert lab.owner.health["run_group_rejected"] == 1
        lab.feed()
        await lab.stored()
        assert run_of(lab.primary) == (None, None), "the run exists without the group that was turned away"
        await lab.stop()
        assert count(lab.primary, "state_event", "kind='RUN_START'") == 0
        (started,) = rows(lab.primary, "SELECT started_wall FROM run")
        assert started["started_wall"] == lab.owner.started_wall
        assert run_of(lab.primary)[1] == "NORMAL"


# ================================================================================================== across databases


async def test_a_run_that_moved_to_the_emergency_database_and_stopped_normally_is_no_abnormal_exit_of_the_one_it_left(
    archive_db_factory, monkeypatch
):
    """R03a 5.2-5 with the real stop: the end is registered in the database the writer uses at that moment."""
    lab = Lab(archive_db_factory, monkeypatch)
    async with lab:
        lab.feed()
        await lab.stored()
        lab.disks.used[lab.config.storage.primary_path] = 0.96  # the primary volume is nearly full: the next groups go on
        lab.feed()
        await lab.until(lambda: not lab.ua.inflight, "the next data is confirmed")
        await lab.stop()
        assert lab.router.mode == "EMERGENCY"
        assert run_of(lab.primary) == (None, None), "the end is in the other database"
        assert run_of(lab.emergency)[1] == "NORMAL"
        router = restart(lab)
        assert run_of(lab.primary) == (None, None) and router.health["primary_closed_runs"] == []
        assert [x["end_reason"] for x in segments_of(lab.primary)] == [None], "a segment closed elsewhere is not cut here"
        assert [x["end_reason"] for x in segments_of(lab.emergency)] == ["SHUTDOWN"]
