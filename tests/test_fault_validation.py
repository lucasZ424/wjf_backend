"""Small, repeatable failure tests. No cluster shutdown, disk filling or field devices."""

import asyncio
import json
import os
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import psycopg
import pytest
from test_audit_regressions import release
from test_storage import frame

from htsg.engine import Engine, Event
from htsg.runtime import Runtime
from htsg.storage import Router


async def until(predicate):
    async with asyncio.timeout(8):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("committed", [False, True])
async def test_writer_retries_unknown_commit_without_duplicates(settings, databases, monkeypatch, committed):
    store = databases[0]
    router = Router(settings, *databases)
    router.initialize()
    runtime = Runtime(settings, router)
    event = asdict(Event("e", "p", "r", settings.config_id, 1, 2, revision=2))
    records = [
        (
            "sample",
            {"event_id": "e", "event": event, "sample": {"id": "s", "point_id": "x", "effective_time": 2}},
        ),
        ("frame", {"frame": frame(settings, "r:2", 2), "event_ids": ["e"]}),
    ]
    real, attempts = store.write_batch, []

    def uncertain(batch):
        attempts.append([p.get("sample", {}).get("id") for _, p in batch])
        if len(attempts) == 1:
            if committed:
                real(batch)
                newer = {**event, "revision": 3, "phase": "CLOSED"}
                store.write("event", newer)
            store._local.connection.close()
            raise psycopg.OperationalError("injected loss of commit acknowledgement")
        return real(batch)

    # Use real() for the independent newer revision to avoid re-entering the fault wrapper.
    def write_event(kind, payload):
        real([(kind, payload)])

    monkeypatch.setattr(store, "write", write_event)
    monkeypatch.setattr(store, "write_batch", uncertain)
    runtime.events.extend(records)
    try:
        await asyncio.wait_for(runtime.writer(), 8)
        assert attempts == [["s", None], ["s", None]]
        assert runtime.health["write_failures"] == 1 and runtime.health["archive_dropped"] == 0
        assert len(store.query("samples", event_id="e")) == len(store.query("products", event_id="e")) == 1
        with store.session() as conn:
            assert conn.execute("SELECT last_seq,retained FROM source_counter").fetchone() == {
                "last_seq": 1,
                "retained": 1,
            }
        assert store.query("event", event_id="e")[0]["payload"]["revision"] == (3 if committed else 2)
    finally:
        release(runtime)


async def test_real_connection_termination_reconnects_without_stopping_detection(settings, databases):
    runtime = Runtime(settings, Router(settings, *databases))
    await runtime.start()
    try:

        def pid():
            with databases[0].session() as conn:
                return conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]

        backend = await asyncio.get_running_loop().run_in_executor(runtime.io, pid)
        with databases[0].connect() as admin:
            assert admin.execute("SELECT pg_terminate_backend(%s) AS killed", (backend,)).fetchone()["killed"]
        runtime.submit("x", 10)
        await until(lambda: runtime.engine.seq > 0)
        await until(lambda: runtime.health["write_failures"] > 0)
        await until(lambda: runtime.health["last_write"] is not None and runtime.accepting)
        runtime.submit("x", 12)
        await until(lambda: runtime.engine.states["x"].state == "HIGH")
        assert not runtime.failed.is_set()
        # A completed empty Pre turn can precede the retried live-event transaction.
        async with asyncio.timeout(8):
            while not (await runtime.db_call(runtime.router.merged, "replay", point="x"))["items"]:
                await asyncio.sleep(0.01)
    finally:
        await runtime.stop()


@pytest.mark.parametrize("stage", ["PENDING", "CONFIRMED", "RECOVERED"])
async def test_killed_capture_process_preserves_commits_and_restart_marks_interrupted(
    settings, databases, stage
):
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "tests/fault_capture_worker.py",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    try:
        # DSN travels only through stdin, never process arguments, logs or checked-in files.
        child.stdin.write(
            json.dumps({"dsn": databases[0].dsn, "config": settings.model_dump(), "stage": stage}).encode()
            + b"\n"
        )
        await child.stdin.drain()
        ready = json.loads(await asyncio.wait_for(child.stdout.readline(), 8))
        assert ready["stage"] == stage
        child.kill()
        await asyncio.wait_for(child.wait(), 8)
        router = Router(settings, *databases)
        router.initialize()
        saved = router.merged("faults")["items"]
        assert len(saved) == (0 if stage == "PENDING" else 1)
        if saved:
            assert saved[0]["payload"]["id"] == ready["id"]
        with databases[0].session() as conn:
            assert not conn.execute("SELECT id FROM source_sample WHERE id='uncommitted'").fetchall()
        assert databases[0].query("replay", point="x")
        fresh = Engine(settings, lambda *args: None, time.time(), time.monotonic())
        assert fresh.run_id != ready["run_id"] and not any(f.recorded for f in fresh.faults.values())
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


async def test_blocked_writer_bounds_queues_and_recovers_with_visible_loss(settings, databases, monkeypatch):
    settings = settings.model_copy(
        update={
            "limits": settings.limits.model_copy(
                update={"event_queue": 16, "ordinary_queue": 4, "ingress": 16, "capture_budget_bytes": 2048}
            )
        }
    )
    router = Router(settings, *databases)
    runtime = Runtime(settings, router)
    await runtime.start()
    gate, entered = threading.Event(), threading.Event()
    real = router.write_batch

    def blocked(records):
        entered.set()
        if not gate.wait(5):
            raise AssertionError("test writer was not released")
        return real(records)

    monkeypatch.setattr(router, "write_batch", blocked)
    try:
        runtime.submit("x", 10)
        await until(entered.is_set)
        for _ in range(30):
            for n in range(30):
                runtime.submit("x", 10 + n)
            await asyncio.sleep(0.01)
            assert len(runtime.events) <= 16 and len(runtime.ordinary) <= 4
            assert runtime.ingress.qsize() <= 16
        assert runtime.engine.seq > 0 and runtime.health["ingress_dropped"] > 0
        assert runtime.health["archive_dropped"] > 0 and not runtime.failed.is_set()
        gate.set()
        await until(lambda: runtime.accepting and not runtime.events)
        await until(lambda: not runtime.event_losses and runtime.loss_summary is None)
        await until(lambda: not runtime.events)
        result = await runtime.db_call(router.merged, "gaps")
        assert any(
            r["payload"]["reason"] in ("INGRESS_OVERFLOW", "ARCHIVE_QUEUE_FULL") for r in result["items"]
        )
    finally:
        gate.set()
        await runtime.stop()


def test_real_cleanup_lock_timeout_rolls_back_then_recovers(settings, databases):
    settings = settings.model_copy(
        update={"storage": settings.storage.model_copy(update={"cleanup_enabled": True})}
    )
    store = databases[0]
    store.write("frame", {"frame": frame(settings, "protected", 1), "event_ids": []})
    router = Router(settings, *databases)
    with store.connect() as locker:
        locker.execute("LOCK TABLE frame_value IN ACCESS EXCLUSIVE MODE")
        router.maintain()
        assert router.health["primary"] == "MAINTENANCE_TIMEOUT"
    router.maintain()
    assert router.health["primary"] == "OK"
    assert store.query("history")[0]["id"] == "protected"


def test_schema_upgrade_failure_is_atomic_and_retryable(settings, databases, monkeypatch):
    store = databases[0]
    store.write("frame", {"frame": frame(settings, "kept", 1), "event_ids": []})
    read = Path.read_text

    def broken(path, *args, **kwargs):
        content = read(path, *args, **kwargs)
        if path.name == "schema.sql":
            return content + "\nCREATE TABLE fault_upgrade_marker(id integer); SELECT 1/0;"
        return content

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_text", broken)
        with pytest.raises(psycopg.errors.DivisionByZero):
            store.initialize(settings)
    with store.session() as conn:
        assert conn.execute("SELECT to_regclass('fault_upgrade_marker') AS t").fetchone()["t"] is None
    store.initialize(settings)
    assert store.query("history")[0]["id"] == "kept"


async def test_fault_queue_bounds_and_reports_rejected_confirmations(settings, databases):
    settings = settings.model_copy(update={"limits": settings.limits.model_copy(update={"event_queue": 4})})
    router = Router(settings, *databases)
    router.initialize()
    runtime = Runtime(settings, router)
    runtime.running = True
    try:
        for i in range(12):
            runtime.emit("fault", {"id": f"f{i}", "point_id": "x", "time": i, "run_id": "r"}, True)
        assert len(runtime.events) == 4
        assert runtime.health["fault_archive_dropped"] == 8
        runtime.running = False
        await asyncio.wait_for(runtime.writer(), 8)
        assert len(databases[0].query("faults")) == 4
    finally:
        release(runtime)


async def test_stalled_calculation_bounds_queue_and_keeps_alarm_detection(settings, monkeypatch):
    import htsg.calculation

    runtime = Runtime(settings, None)
    runtime.running = True
    runtime.engine = Engine(settings, runtime.emit, 1000, 0, defer_calculation=True)
    gate, entered = threading.Event(), threading.Event()
    real = htsg.calculation.calculate

    def blocked(*args):
        entered.set()
        if not gate.wait(5):
            raise AssertionError("test calculation was not released")
        return real(*args)

    monkeypatch.setattr(htsg.calculation, "calculate", blocked)
    task = asyncio.create_task(runtime.calculator())
    try:
        runtime.engine.tick(1001.3, 1.3)
        await until(entered.is_set)
        for n in range(2, 10):
            runtime.engine.tick(1000 + n + 0.3, n + 0.3)
            assert runtime.calc_queue.qsize() <= 4
        for t in range(6):
            runtime.engine.receive("x", 10, 1009.4 + t, 9.4 + t)
        assert runtime.engine.states["x"].state == "HIGH"
        assert any(k == "fault" for k, _ in runtime.events)
        assert runtime.engine.latest_frame["derived_results"]["delta"]["reason"] == "CALCULATION_QUEUE_FULL"
        gate.set()
        await until(lambda: not runtime.ingress.empty())
    finally:
        gate.set()
        runtime.running = False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        release(runtime)
