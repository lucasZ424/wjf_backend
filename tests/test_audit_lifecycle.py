"""Accelerated production-duration lifecycle and additive schema upgrade contracts."""

import asyncio
import time

from test_audit_regressions import pages, release
from test_engine import put
from test_storage import frame

from htsg.engine import Engine
from htsg.runtime import Runtime
from htsg.storage import Router


async def test_full_capture_cycle_with_concurrent_pre_drain(settings, databases, record_property):
    settings = settings.model_copy(
        update={
            "profiles": (settings.profiles[0].model_copy(update={"pre": 600, "recovery": 60, "post": 1200}),)
        }
    )
    router = Router(settings, *databases)
    router.initialize()
    runtime = Runtime(settings, router)
    runtime.running = True
    runtime.engine = Engine(settings, runtime.emit, 1000, 0)
    writer = None
    try:
        for n in range(3000):
            for point in ("x", "y"):
                put(runtime.engine, point, 2, n / 5)
            runtime.engine.tick(1000 + n / 5, n / 5)
            runtime.ordinary.clear()
        put(runtime.engine, "x", 10, 600)
        event = runtime.engine.active["p"]
        expected = {s.id for s in runtime.capture_jobs[0]["plan"].samples}
        expected.add(runtime.engine.states["x"].latest.id)
        started = time.perf_counter()
        writer = asyncio.create_task(runtime.writer())
        peak_queue = 0
        # Virtual acquisition is faster than real time. Pause the producer at a small
        # backlog, never enlarge the production queue to conceal lost observations.
        for n in range(6307):
            t = 600.1 + n / 5
            for point in ("x", "y"):
                active_before = bool(runtime.engine.active)
                sample = put(runtime.engine, point, 2, t)
                if active_before:
                    expected.add(sample.id)
            runtime.engine.tick(1000 + t, t)
            while not runtime.ingress.empty():
                kind, item = runtime.ingress.get_nowait()
                assert kind == "capture_done"
                runtime.engine.capture_complete(*item)
                runtime.ingress.task_done()
            peak_queue = max(peak_queue, len(runtime.events))
            while len(runtime.events) > 128:
                assert not writer.done(), writer.exception()
                await asyncio.sleep(0.002)
            if n % 5 == 0:
                await asyncio.sleep(0)
        runtime.running = False
        runtime.write_ready.set()
        await asyncio.wait_for(writer, 60)
        record_property("accelerated_cycle_seconds", time.perf_counter() - started)
        record_property("peak_live_event_queue", peak_queue)
        archived = pages(router, "samples", event_id=event.id)
        assert {r["payload"]["id"] for r in archived} == expected
        saved = router.merged("event", event_id=event.id)["items"][0]
        assert saved["payload"]["close_reason"] == "RECOVERED"
        assert saved["payload"]["archive_status"] == "SUBMITTED"
        assert not saved["has_gaps"] and saved["payload"]["coverage"] == "OBSERVED"
        assert event.end >= event.recovered_at + 1200
        assert not runtime.engine.pending and not runtime.capture_jobs
        assert runtime.health["archive_dropped"] == runtime.health["write_failures"] == 0
    finally:
        runtime.running = False
        if writer and not writer.done():
            writer.cancel()
            await asyncio.gather(writer, return_exceptions=True)
        release(runtime)


def test_upgrade_existing_source_history_keeps_order_and_coverage(settings, databases):
    store = databases[0]
    # Recreate precisely the v1 absence of counter metadata, retaining real history.
    for i in range(5):
        store.write("frame", {"frame": frame(settings, f"r:{i}", 100 - i), "event_ids": []})
    with store.session() as conn:
        conn.execute("DROP TRIGGER source_insert_count ON frame_value")
        conn.execute("DROP TRIGGER source_delete_count ON frame_value")
        conn.execute("DELETE FROM schema_version WHERE version=2")
        conn.execute("TRUNCATE source_counter")
        conn.execute("UPDATE frame_value SET source_seq=NULL,good_prefix=NULL")
    store.initialize(settings)
    store.initialize(settings)
    assert store.metrics(settings.points)["seed"][0]["covered_seconds"] == 5
    store.cleanup(1000, pressure=True, seed_count=3, month_seconds=10)
    # Clock rollback must not turn wall-time ordering into the seed replacement order.
    assert {r["id"] for r in store.query("history")} == {"r:2", "r:3", "r:4"}
    store.write("frame", {"frame": frame(settings, "r:4", 96), "event_ids": []})
    with store.session() as conn:
        counter = conn.execute("SELECT * FROM source_counter WHERE point_id='x'").fetchone()
        assert counter["last_seq"] == 5 and counter["retained"] == 3
        assert counter["retained_good"] == 3
