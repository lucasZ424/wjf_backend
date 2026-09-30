"""Regression contracts for audit sections 8-11; clocks and watermarks never fill disks."""

import asyncio
import json
import time
from dataclasses import asdict

import psycopg
import pytest
from test_engine import engine, put
from test_storage import frame

from htsg.engine import Engine, Event
from htsg.runtime import Runtime
from htsg.storage import PolicyRefusal, Router


def release(runtime):
    runtime.io.shutdown(wait=True)
    runtime.read_io.shutdown(wait=True)
    runtime.calc_io.shutdown(wait=True)


def pages(router, category, **kwargs):
    cursor, items = None, []
    while True:
        page = router.merged(category, limit=137, cursor=cursor, **kwargs)
        assert not page["partial"]
        items.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            assert not page["has_more"]
            return items


@pytest.mark.parametrize("phase", [0.1, 0.6])
def test_hold_survives_next_bucket_arriving_before_seal(settings, phase):
    e, _ = engine(settings)
    for cycle in range(10):
        t = cycle * 2 + phase
        put(e, "x", 2, t)
        e.tick(1000 + t, t)
        e.tick(1001.3 + cycle * 2, 1.3 + cycle * 2)
        # At phase .1 the following bucket has already arrived before sealing the empty one.
        if phase < 0.2:
            put(e, "x", 2, cycle * 2 + 2.1)
        e.tick(1002.3 + cycle * 2, 2.3 + cycle * 2)
        assert e.latest_frame["values"]["x"]["basis"] == "HELD_VALUE"
        assert e.latest_frame["values"]["x"]["value"] == 2


def test_future_normal_never_advances_frontier_or_recovers_alarm(settings):
    settings = settings.model_copy(update={"time_basis": "source"})
    e, records = engine(settings)
    high = put(e, "x", 10, 0.1, source_time=1000.1)
    put(e, "y", 2, 0.1, source_time=1000.1)
    bad = put(e, "x", 2, 0.2, source_time=1010)
    assert bad.quality == "BAD_TIME" and e.states["x"].frontier == high
    assert e.active["p"].phase == "ACTIVE"
    assert e.active["p"].coverage == "PARTIAL"
    put(e, "x", 2, 0.3, source_time=1000.3)
    assert e.active["p"].phase == "RECOVERY_PENDING"
    assert any(k == "gap" and p["reason"] == "FUTURE_SOURCE_TIME" and p["event_id"] for k, p, _ in records)


def test_known_loss_marks_current_and_subsequent_pre_event(settings):
    e, records = engine(settings)
    e.note_loss("x", "INGRESS_OVERFLOW", 999, 1000, 4)
    put(e, "x", 10, 0.1)
    event = e.active["p"]
    event.coverage = "OBSERVED"
    e.note_loss("x", "INGRESS_OVERFLOW", 1001, 1002, 2)
    assert event.coverage == "PARTIAL" and e.health["lost_observations"] == 6
    assert any(
        k == "gap" and p["event_id"] == event.id and p["reason"] == "PRE_SOURCE_LOSS" for k, p, _ in records
    )
    assert any(k == "gap" and p["event_id"] == event.id and p.get("point_id") == "x" for k, p, _ in records)


async def test_resource_recovery_does_not_erase_duration_suppression(settings):
    runtime = Runtime(settings, None)
    runtime.running = True
    runtime.engine.capture_available = False
    runtime.engine.blocked = {"p": "CAPTURE_DURATION_LIMIT", "other": "RESOURCE_LIMITED"}
    task = asyncio.create_task(runtime.owner())
    try:
        await asyncio.sleep(0.07)
        assert runtime.engine.blocked == {"p": "CAPTURE_DURATION_LIMIT"}
        runtime.record_event_loss("event", {"id": "closed-id"}, "TEST_DROP")
        assert "closed-id" in runtime.event_losses
    finally:
        runtime.running = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release(runtime)


def test_out_of_order_frame_completion_waits_for_all_associations(settings):
    e, records = engine(settings)
    put(e, "x", 10, 0.1)
    event = e.active["p"]
    old, tail = e.make_frame(1000), e.make_frame(1001)
    e.close_event(event, 1001.5, "RECOVERED")
    e.capture_pending.add(event.id)
    e.finish_frame(tail)
    e.capture_complete(event.id, True)
    assert event in e.pending and event.archive_status == "PENDING"
    e.finish_frame(old)
    assert event not in e.pending and event.archive_status == "SUBMITTED"
    archived = [p for k, p, _ in records if k == "frame"]
    assert all(event.id in p["event_ids"] for p in archived)
    assert not archived[-1]["display"]


@pytest.mark.parametrize("policy", [True, False])
async def test_writer_distinguishes_policy_refusal_from_programming_bug(settings, policy):
    class BrokenRouter:
        def write_batch(self, records):
            raise PolicyRefusal("TEST_BUDGET") if policy else RuntimeError("BUG")

    runtime = Runtime(settings, BrokenRouter())
    runtime.ordinary.append(("frame", {"frame": {}, "event_ids": []}))
    try:
        if policy:
            await runtime.writer()
            assert runtime.health["archive_dropped"] == 1 and runtime.loss_summary[0] == "TEST_BUDGET"
        else:
            runtime.running = True
            owner = asyncio.create_task(asyncio.sleep(60))
            writer = asyncio.create_task(runtime.writer())
            with pytest.raises(RuntimeError, match="BUG"):
                await writer
            runtime.task_completed(writer)
            assert runtime.failed.is_set() and runtime.health["fatal_worker"] == "RuntimeError"
            runtime.router.close = lambda: None
            runtime.tasks = [owner, writer]
            await runtime.stop()
            assert runtime.health["archive_dropped"] == 0
    finally:
        release(runtime)


async def test_production_pre_snapshot_drains_through_real_writer(settings, databases, record_property):
    settings = settings.model_copy(
        update={"profiles": (settings.profiles[0].model_copy(update={"pre": 600}),)}
    )
    router = Router(settings, *databases)
    router.initialize()
    runtime = Runtime(settings, router)
    runtime.running = True
    runtime.engine = Engine(settings, runtime.emit, 1000, 0)
    try:
        # Warm 600 seconds at 5 Hz per point, without a large archive queue of prior history.
        for n in range(3000):
            t = n / 5
            put(runtime.engine, "x", 2, t)
            put(runtime.engine, "y", 2, t)
            runtime.engine.tick(1000 + t, t)
            runtime.ordinary.clear()
        started = time.perf_counter()
        put(runtime.engine, "x", 10, 600)
        record_property("pre_owner_seconds", time.perf_counter() - started)
        event = runtime.engine.active["p"]
        plan = runtime.capture_jobs[0]["plan"]
        expected = {s.id for s in plan.samples}
        assert len(expected) == 6000 and len(runtime.capture_jobs) == 1
        assert runtime.capture_bytes <= settings.limits.capture_budget_bytes
        assert runtime.health["archive_dropped"] == 0 and runtime.accepting
        assert len(runtime.events) < 20
        frozen_revision = json.loads(plan.event_json)["revision"]
        event.revision += 1
        for state in runtime.engine.states.values():
            state.ring.clear()
        assert plan.batch(0, 1)[0][1]["event"]["revision"] == frozen_revision
        runtime.running = False
        started = time.perf_counter()
        await asyncio.wait_for(runtime.writer(), 60)
        record_property("pre_writer_drain_seconds", time.perf_counter() - started)
        samples = pages(router, "samples", event_id=event.id)
        assert expected <= {r["payload"]["id"] for r in samples}
        assert len(samples) == 6001
        assert len(pages(router, "products", event_id=event.id)) == len(plan.frames)
        assert runtime.capture_bytes == 0 and not runtime.capture_jobs
        assert runtime.health["archive_dropped"] == 0 and runtime.health["write_failures"] == 0
    finally:
        release(runtime)


def test_pre_budget_rejection_is_explicit(settings):
    settings = settings.model_copy(
        update={"limits": settings.limits.model_copy(update={"capture_budget_bytes": 1024})}
    )
    runtime = Runtime(settings, None)
    runtime.running = True
    runtime.engine = Engine(settings, runtime.emit, 1000, 0)
    try:
        for n in range(5):
            put(runtime.engine, "x", 2, n)
        put(runtime.engine, "x", 10, 5)
        assert not runtime.capture_jobs and runtime.capture_bytes == 0
        assert any(k == "gap" and p["reason"] == "PRE_BUDGET_EXHAUSTED" for k, p in runtime.events)
        assert runtime.engine.active["p"].coverage == "PARTIAL"
    finally:
        release(runtime)


def test_batch_rollback_retry_and_connection_reuse(settings, databases, monkeypatch):
    store = databases[0]
    store.close()
    real_connect, connections = store.connect, []

    def connect():
        conn = real_connect()
        connections.append(conn)
        return conn

    monkeypatch.setattr(store, "connect", connect)
    record = ("frame", {"frame": frame(settings, "r:1", 100), "event_ids": []})
    with pytest.raises(ValueError, match="unknown record"):
        store.write_batch([record, ("typo", {})])
    assert not store.query("history")
    store.write_batch([record, record])
    store.write_batch([record])  # replay after an uncertain commit must be harmless
    assert len(store.query("history")) == 1
    with store.session() as conn:
        counter = conn.execute("SELECT * FROM source_counter").fetchone()
        assert counter["last_seq"] == counter["retained"] == 1
    assert len(connections) == 1


@pytest.mark.parametrize("fail_after_probe", [False, True])
def test_switchback_failure_keeps_raw_in_emergency(settings, databases, monkeypatch, fail_after_probe):
    router = Router(settings, *databases)
    router.initialize()
    router.mode = "EMERGENCY"
    databases[1].mode("EMERGENCY")

    def readonly(*args):
        raise psycopg.errors.ReadOnlySqlTransaction("simulated read-only primary")

    monkeypatch.setattr(databases[0], "write_batch" if fail_after_probe else "probe_write", readonly)
    monkeypatch.setattr(
        "shutil.disk_usage", lambda p: type("Usage", (), {"total": 100 * 1024**3, "free": 90 * 1024**3})()
    )
    record = ("sample", {"event_id": "e", "sample": {"id": "s", "point_id": "x", "effective_time": 100}})
    router.write_batch([record])
    assert router.mode == databases[1].mode() == "EMERGENCY"
    assert len(databases[1].query("samples", event_id="e")) == 1
    assert not databases[0].query("samples", event_id="e")


def test_cross_store_keyset_is_complete_scoped_and_stable(settings, databases):
    router = Router(settings, *databases)
    records = [
        ("sample", {"event_id": "e", "sample": {"id": f"s{i:05}", "point_id": "x", "effective_time": i // 3}})
        for i in range(6005)
    ]
    for store, subset in [(databases[0], records[:4000]), (databases[1], records[3000:])]:
        for i in range(0, len(subset), 128):
            store.write_batch(subset[i : i + 128])
    items = pages(router, "samples", event_id="e")
    assert [r["payload"]["id"] for r in items] == [f"s{i:05}" for i in range(6005)]
    first = router.merged("samples", event_id="e", limit=2)
    with pytest.raises(ValueError, match="another query"):
        router.merged("samples", event_id="different", cursor=first["next_cursor"])
    for i in [4, 2, 5, 1, 3]:
        databases[i % 2].write("event", asdict(Event(str(i), "p", "r", settings.config_id, 0, i)))
    assert [r["payload"]["id"] for r in pages(router, "events")] == ["5", "4", "3", "2", "1"]


def test_cleanup_drains_aging_backlog_and_preserves_seed(settings, databases, record_property):
    store, now = databases[0], time.time()
    records = [
        ("frame", {"frame": frame(settings, f"r:{i}", now - 10000 + i), "event_ids": []}) for i in range(3200)
    ]
    for i in range(0, len(records), 128):
        store.write_batch(records[i : i + 128])
    counts, elapsed = [], []
    for step in range(4):
        # Continuous incoming history coexists with cleanup of the older cohort.
        store.write_batch(
            [
                ("frame", {"frame": frame(settings, f"new:{step}:{i}", now + step), "event_ids": []})
                for i in range(10)
            ]
        )
        started = time.perf_counter()
        counts.append(
            store.cleanup(
                now + step, pressure=True, seed_count=30, month_seconds=10, batch=1024, budget_ms=200
            )["ordinary_removed"]
        )
        elapsed.append(time.perf_counter() - started)
    assert sum(counts) == 3200 and max(counts) == 1024
    assert len(store.query("history", limit=1000)) == 40
    assert store.metrics(settings.points)["seed"][0]["covered_seconds"] == 40
    record_property("cleanup_max_seconds", max(elapsed))


def test_maintenance_health_recovers_from_timeout(settings, databases, monkeypatch):
    settings = settings.model_copy(
        update={"storage": settings.storage.model_copy(update={"cleanup_enabled": True})}
    )
    router = Router(settings, *databases)
    real = databases[0].cleanup

    def timeout(*args, **kwargs):
        raise psycopg.errors.QueryCanceled("test timeout")

    monkeypatch.setattr(databases[0], "cleanup", timeout)
    router.maintain()
    assert router.health["primary"] == "MAINTENANCE_TIMEOUT"
    monkeypatch.setattr(databases[0], "cleanup", real)
    router.maintain()
    assert router.health["primary"] == "OK"
