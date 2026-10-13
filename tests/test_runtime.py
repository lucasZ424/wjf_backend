"""D45: the runtime assembles the batch-driven chain and nothing else (方案模块 H).

Real Modbus and OPC UA servers at full scale (8 bridges, 200 monitored items), real PostgreSQL archives, the
real owner, queue and web application.  Why it matters: this is the wiring the delivered system runs on, so
what the pieces promise one by one - ordered ingress, whole-group commits, realtime state that does not wait for
the database, an honest end of the run - must also hold once they are connected.
"""

import asyncio
import subprocess
import sys
import time
from pathlib import Path

import httpx
import psycopg
import pytest
from live_support import PROCESS, UA_NAMES, full_config
from protocol_support import FieldModbus, FieldUa
from test_archive_router import Disks

from htsg import runtime as runtime_module
from htsg.archive import ArchiveStore
from htsg.archive_router import ArchiveRouter
from htsg.auth import Accounts
from htsg.config import Timing
from htsg.runtime import Runtime
from htsg.web import create_web_app

TIMING = Timing(ua_keepalive_count=1)  # the asyncua test server sends its keep-alive one cycle later than the standard says


class Field:
    """The two protocol servers with distinct, plausible values for every channel."""

    async def __aenter__(self):
        self.mb, self.ua = FieldModbus(), FieldUa(UA_NAMES)
        await self.mb.start()
        await self.ua.start()
        for i, name in enumerate(UA_NAMES):
            value = 480.0 + i * 0.01 if name.startswith("temp_inner") else 440.0 + i * 0.01 if name.startswith("temp_outer") else PROCESS[name]
            await self.ua.set(name, value)
        self.config = full_config(self.mb.port, self.ua.endpoint, TIMING)
        return self

    async def __aexit__(self, *exc):
        await self.ua.stop()
        await self.mb.stop()


async def until(condition, why, timeout=30):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out: {why}"
        await asyncio.sleep(0.1)


def build(field, tmp_path, primary, emergency):
    router = ArchiveRouter(field.config, primary, emergency, disk_usage=Disks())
    accounts = Accounts(tmp_path / "accounts.json")
    credentials = {"viewer": accounts.create("viewer", "viewer"), "admin": accounts.create("admin", "admin")}
    runtime = Runtime(field.config, router, accounts)
    app = create_web_app(
        config=field.config, accounts=accounts, publisher=runtime.publisher, archive_router=router,
        queue=runtime.queue, diagnostics=runtime.diagnostics, lifecycle=runtime,
    )  # fmt: skip
    return runtime, app, credentials


def kinds(store, kind):
    with psycopg.connect(store.dsn) as conn:
        return conn.execute("SELECT count(*) FROM state_event WHERE kind=%s", (kind,)).fetchone()[0]


async def test_the_runtime_reads_both_protocols_archives_serves_the_history_and_ends_the_run(archive_db_factory, tmp_path):
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    async with Field() as field:
        runtime, app, credentials = build(field, tmp_path, primary, emergency)
        await runtime.start()
        try:
            await until(lambda: all(v["valid"] for v in runtime.owner.channel_views().values()), "all 208 inputs and 209 results are valid")
            assert len(runtime.owner.channel_views()) == 417
            await until(lambda: runtime.queue.depth == 0 and runtime.queue.stats["committed"] > 3, "the archive has written")
            headers = {"Authorization": "Bearer " + credentials["viewer"]}
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://htsg") as client:
                health = (await client.get("/api/health", headers=headers)).json()
                assert health["runtime"]["accounts"] == "DATABASE_AUTHORITATIVE" and health["runtime"]["fatal"] is None
                granted = health["negotiated"]["ua_field"]
                assert granted["keepalive_count"] == TIMING.ua_keepalive_count and granted["requested"]["queue_size"] == 10
                assert granted["violations"] == [], "what the server really granted was checked against the limits"
                assert {s: v["archive"] for s, v in health["archive"]["sources"].items()} == {"mb_field": "OK", "ua_field": "OK"}
                assert health["archive"]["queue"]["committed"] > 0 and health["archive"]["queue"]["failures"] == 0
                assert len((await client.get("/api/channels", headers=headers)).json()["channels"]) == 417
                query = {"center": time.time(), "seconds": 120, "points": "bridge_mvv_01,strain_01,temp_inner_01,delta_t_01"}
                body = (await client.get("/api/replay", params=query, headers=headers)).json()
                rows = {pid: [x for s in body["channels"][pid]["segments"] for x in s["rows"]] for pid in body["channels"]}
                assert len(rows["bridge_mvv_01"]) >= 2, "a Modbus round is stored every second"
                assert len(rows["temp_inner_01"]) == 1, "a value that never changes is stored once, then only restated"
                by_batch = {x["batch_seq"]: x["value"] for x in rows["bridge_mvv_01"] if x["origin"] == "RECEIVED"}
                strains = [x for x in rows["strain_01"] if x["origin"] == "RECEIVED"]
                assert len(strains) == 1, "a result is stored when it changes: the steady bridge gives one"
                for strain in strains:
                    assert strain["value"] == pytest.approx(by_batch[strain["batch_seq"]] * 1000 / 6), "a result with ITS input"
                inner, outer = 480.0 + UA_NAMES.index("temp_inner_01") * 0.01, 440.0 + UA_NAMES.index("temp_outer_01") * 0.01
                assert rows["delta_t_01"][0]["value"] == pytest.approx(inner - outer)
        finally:
            await runtime.stop()
    assert runtime.health["unwritten_at_stop"] == 0, "everything accepted was written before the process ended"
    assert kinds(primary, "RUN_START") == 1 and kinds(primary, "RUN_END") == 1, "the run is registered and ended"


async def test_realtime_state_does_not_wait_for_a_database_that_cannot_be_reached(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_module, "STOP_WRITE_WAIT", 0.5)
    dead = "host=127.0.0.1 port=1 dbname=htsg user=nobody connect_timeout=1"
    async with Field() as field:
        runtime, _, _ = build(field, tmp_path, ArchiveStore(dead), ArchiveStore(dead))
        await runtime.start()
        try:
            await until(lambda: all(v["valid"] for v in runtime.owner.channel_views().values()), "the live state is complete without any archive")
            await until(lambda: runtime.queue.stats["failures"] + runtime.queue.stats["unknown"] > 0, "the failed writes are counted")
            assert runtime.health["accounts"] == "LOCAL_VERIFICATION_CACHE_ONLY" and runtime.health["fatal"] is None
            assert not runtime.failed.is_set(), "no task of the chain died because the archive is away"
        finally:
            await runtime.stop()
    assert runtime.health["unwritten_at_stop"] > 0, "what could not be written is reported at the end, not hidden"


OLD_CHAIN = ("htsg.engine", "htsg.calculation", "htsg.faults", "htsg.adapters", "htsg.storage", "htsg.capture", "htsg.api", "htsg.research_api")


def test_the_old_frame_chain_is_gone_from_the_package_and_the_old_schema_is_not_shipped():
    """D45 ②④: the calculator, frame, window and profile mechanisms no longer exist to be started, and the old
    frame-era schema is not part of what is installed."""
    import importlib.util

    assert [name for name in OLD_CHAIN if importlib.util.find_spec(name) is not None] == []
    assert not (Path(__file__).resolve().parents[1] / "htsg" / "schema.sql").exists()
    assert "schema.sql" not in (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8").replace("archive_schema.sql", "")


def test_the_delivered_entry_points_load_none_of_the_old_frame_chain():
    """D45 ②: the calculator, frame, window and profile mechanisms are not reachable from the way the system starts."""
    old = set(OLD_CHAIN)
    code = (
        "import sys, htsg.cli, htsg.runtime, htsg.web, htsg.archive, htsg.archive_router, htsg.modbus, htsg.ua, htsg.owner; "
        f"print(sorted(m for m in sys.modules if m in {old!r}))"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]", f"the old chain was imported: {done.stdout}"


async def test_a_core_task_that_dies_is_reported_as_the_failure_the_supervisor_acts_on(archive_db_factory, monkeypatch):
    """The command line stops the process with a failure code when this fires, and the task scheduler restarts it."""
    from owner_support import small_config

    config = small_config(sources=[{"id": "mb", "kind": "modbus"}, {"id": "ua", "kind": "opcua"}])
    router = ArchiveRouter(config, archive_db_factory("primary"), archive_db_factory("emergency"), disk_usage=Disks())
    runtime = Runtime(config, router)

    def broken(wall, mono):
        raise RuntimeError("boom")

    monkeypatch.setattr(runtime.owner, "tick", broken)
    await runtime.start()
    try:
        await asyncio.wait_for(runtime.failed.wait(), 5)
        assert runtime.health["fatal"] == "owner loop: RuntimeError"
    finally:
        await runtime.stop()
