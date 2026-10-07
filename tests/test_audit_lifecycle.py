"""Accelerated production-duration lifecycle and additive schema upgrade contracts."""

import asyncio

from test_audit_regressions import pages, release
from test_engine import put
from test_storage import frame

from htsg.engine import Engine
from htsg.runtime import Runtime
from htsg.storage import Router


async def test_fault_cycle_archives_before_during_and_after(settings, databases):
    router = Router(settings, *databases)
    router.initialize()
    runtime = Runtime(settings, router)
    runtime.running = True
    runtime.engine = Engine(settings, runtime.emit, 1000, 0)
    try:
        for t in range(20):
            put(runtime.engine, "x", 10 if 3 <= t <= 10 else 2, t)
            put(runtime.engine, "y", 2, t)
            runtime.engine.tick(1000 + t, t)
        runtime.running = False
        await asyncio.wait_for(runtime.writer(), 8)
        faults = pages(router, "faults")
        assert len(faults) == 1 and faults[0]["payload"]["time"] == 1008
        rows = pages(router, "replay", point="y", start=1000, end=1020)
        assert len(rows) == 20 and all(r["payload"]["value"] == 2 for r in rows)
        assert not pages(router, "events")
        assert runtime.health["archive_dropped"] == runtime.health["write_failures"] == 0
    finally:
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
