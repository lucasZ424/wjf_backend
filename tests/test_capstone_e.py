"""Capstone E: from signal to fault history (方案第 7 节).

Full-scale configuration, deterministic time, a real ordered writer on real PostgreSQL that can be blocked.
One scenario combines: constant violation, a short outage and recovery, a batch with two versions of one
input, two-sided recovery, and an archive outage with a full recovery unit afterwards.
"""

import asyncio
import threading
import time

from live_support import BRIDGES, FULL, NOMINAL, UA_NAMES
from owner_support import FixedBucket
from test_archive_router import Disks

from htsg import records as r
from htsg.archive_router import ArchiveRouter, group_bytes
from htsg.commitq import CommitQueue
from htsg.config import Settings
from htsg.owner import Owner

WALL0, MONO0 = 1000.0, 100.0


def make_config():
    payload = FULL.model_dump(mode="json")
    payload["simulation"]["enabled"] = False
    return Settings.model_validate(payload)


class Scenario:
    def __init__(self, router_factory):
        self.cfg = make_config()
        self.gate = threading.Event()
        self.gate.set()
        self.real = router_factory(self.cfg)
        self.seq = 0
        outer = self

        class Gated:
            def write_group(self, group):
                outer.gate.wait(60)
                return outer.real.write_group(group)

        self.queue = CommitQueue(
            Gated(), 25,
            on_committed=lambda g, res: self.owner.committed(g, res),
            on_refused=lambda g, why: self.owner.refused(g, why),
            is_void=lambda g: self.owner.is_void(g),
            retry_delay=(0.01, 0.05),
        )  # fmt: skip
        self.owner = Owner(self.cfg, WALL0, MONO0, commit=self.queue.offer, buckets=FixedBucket(), run_id="e")
        self.owner.start(WALL0, MONO0)
        for source in ("mb_field", "ua_field"):
            self.owner.begin_generation(source, 1, WALL0, MONO0)
        self.values = dict(NOMINAL)
        self.bridges = {b: 0.7 + 0.001 * k for k, b in enumerate(BRIDGES)}
        self.confirmed = {}  # fault id -> record, as the owner confirmed them

    def at(self, mono):
        return WALL0 + (mono - MONO0), mono

    async def second(self, mono, changes=(), alive=True, mb=True, versions=None, bridges=None):
        wall, mono = self.at(mono)
        self.bridges.update(bridges or {})
        items = []
        if versions:
            items += [(n, v) for n, vs in versions.items() for v in vs]
        for name, value in changes:
            self.values[name] = value
            items.append((name, value))
        self.seq += 1
        if items:
            self.owner.accept(
                r.InputBatch("ua_field", 1, r.KIND_UA_NOTIFY, wall, mono,
                             tuple(r.Item(n, v) for n, v in items), publish_seq=self.seq)
            )  # fmt: skip
        elif alive:
            self.owner.accept(r.InputBatch("ua_field", 1, r.KIND_UA_NOTIFY, wall, mono))
        if mb:
            tick = int(mono - MONO0)
            self.owner.accept(
                r.InputBatch("mb_field", 1, r.KIND_MODBUS_POLL, wall, mono,
                             tuple(r.Item(b, v) for b, v in self.bridges.items()), tick=tick)
            )  # fmt: skip
        before = set(self.owner.fault_buffer)
        self.owner.tick(wall, mono)
        for fid in set(self.owner.fault_buffer) - before:
            self.confirmed[fid] = self.owner.fault_buffer[fid]
        await asyncio.sleep(0.003)

    async def initial(self):
        wall, mono = self.at(101.0)
        items = tuple(r.Item(n, v) for n, v in self.values.items())
        self.owner.accept(r.InputBatch("ua_field", 1, r.KIND_UA_NOTIFY, wall, mono, items, publish_seq=0))

    def stored_faults(self):
        with self.real.primary.session() as conn:
            return {x["id"]: x for x in conn.execute("SELECT * FROM fault_record").fetchall()}


async def test_constant_outage_multiversion_two_sided_and_archive_stop_end_in_a_consistent_fault_history(
    archive_db_factory,
):
    def router_factory(cfg):
        router = ArchiveRouter(cfg, archive_db_factory("primary"), archive_db_factory("emergency"), disk_usage=Disks())
        router.initialize()
        router.bind_run("e", cfg.config_id, WALL0)
        return router

    sc = Scenario(router_factory)
    owner = sc.owner
    writer = asyncio.create_task(sc.queue.run())
    try:
        await sc.initial()
        for t in range(102, 110):
            await sc.second(float(t))
        # 1. a constant violation: only ONE sample of the wall, the clock does the rest (497 > 490), and on the other source a
        # strain beyond its limit (4.2 mV/V is 700 με > 690), the derived quantity with a limit in this configuration
        await sc.second(110.0, changes=[("temp_wall_001", 497.0)], bridges={"bridge_mvv_01": 4.2})
        for t in range(111, 115):
            await sc.second(float(t))
        assert owner.tracker.status("temp_wall_001") == "PENDING", "4 s of the 5 s have passed at 114.0"
        assert owner.tracker.status("strain_01") == "PENDING"
        await sc.second(115.0)
        assert owner.tracker.status("temp_wall_001") == "CONFIRMED" and owner.tracker.status("strain_01") == "CONFIRMED"
        # 2. a violation that starts at 116.0 is cut by a 4 s outage and must start over after the recovery
        await sc.second(116.0, changes=[("temp_wall_002", 497.0)], alive=True, mb=False)
        for t in (117,):
            await sc.second(float(t))
        for t in (118, 119, 120, 121):  # no response from the OPC UA source
            await sc.second(float(t), alive=False)
        assert owner.sources["ua_field"].state == "EXPIRED"
        await sc.second(122.0)  # same subscription resumes inside the 3-6 s window
        assert owner.sources["ua_field"].state == "VALID" and owner.health["resyncs"] == 0
        for t in range(123, 127):
            await sc.second(float(t))
        assert owner.tracker.status("temp_wall_002") == "PENDING", "old clock (116.0) would already be due"
        await sc.second(127.0)
        assert owner.tracker.status("temp_wall_002") == "CONFIRMED"
        # 3. a batch carrying a normal and a violating version of one input: the clock restarts at the batch (C7; the derived
        # case of 终版 §8.2 no longer arises in this configuration and is in test_fault_owner, C8)
        await sc.second(130.0, versions={"temp_wall_003": [480.0, 497.0]})
        for t in range(131, 135):
            await sc.second(float(t))
        assert owner.tracker.status("temp_wall_003") == "PENDING"
        await sc.second(135.0)
        assert owner.tracker.status("temp_wall_003") == "CONFIRMED"
        # 4. two-sided: recovery through the hysteresis line (500 με <= 690 - 55), then the opposite direction (-700 με < -690)
        await sc.second(140.0, changes=[("temp_wall_001", 480.0)], bridges={"bridge_mvv_01": 3.0})
        assert owner.tracker.status("strain_01") == "NORMAL" and owner.tracker.status("temp_wall_001") == "NORMAL"
        await sc.second(145.0, bridges={"bridge_mvv_01": -4.2})
        for t in range(146, 151):
            await sc.second(float(t))
        # settle everything so far, then stop the writer's database
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (
            sc.queue.depth or set(sc.stored_faults()) != set(sc.confirmed)
        ):
            await asyncio.sleep(0.01)
        durable = set(sc.stored_faults())
        assert durable == set(sc.confirmed) and len(durable) == 5, (len(durable), len(sc.confirmed))  # three walls, a strain twice
        strain = sorted((f["confirmed_wall"], f["direction"]) for f in sc.stored_faults().values() if f["point_id"] == "strain_01")
        assert [d for _, d in strain] == ["HIGH", "LOW"], "one record per confirmation: high, recovery, then low"
        # 5. the archive stops: faults keep being confirmed, kept in memory, shown as current, NOT in the history list
        sc.gate.clear()
        before = len(sc.confirmed)
        await sc.second(151.0, changes=[(f"temp_wall_{k:03d}", 497.0) for k in range(4, 11)])
        for t in range(152, 175):
            await sc.second(float(t))
        new_ids = set(sc.confirmed) - durable
        assert len(sc.confirmed) - before == 7, "7 walls were confirmed during the outage"
        status = owner.fault_status()
        assert status["buffered"] == 7 and status["rejected_total"] == 0
        for fid in new_ids:
            record = sc.confirmed[fid]
            assert owner.tracker.confirmed_direction(record.point_id) == "HIGH", "the current page shows it"
        assert set(sc.stored_faults()) == durable, "the history list holds only what was really committed"
        assert any(s.archive != "OK" for s in owner.sources.values()), "the outage opened a gap"
        # 6. storage returns: the recovery unit carries the buffered faults, durability is then confirmed
        sc.gate.set()
        for t in range(175, 190):
            await sc.second(float(t))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (sc.queue.depth or owner.fault_status()["buffered"]):
            await sc.second(190.0 + (time.monotonic() - deadline + 15))
            await asyncio.sleep(0.02)
        assert owner.fault_status()["buffered"] == 0 and sc.queue.depth == 0
        stored = sc.stored_faults()
        assert set(stored) == set(sc.confirmed), "every confirmed fault is durable exactly once"
        assert all(s.archive == "OK" for s in owner.sources.values())
        with sc.real.primary.session() as conn:
            assert conn.execute("SELECT count(*) - count(DISTINCT id) AS d FROM fault_record").fetchone()["d"] == 0
            gaps = conn.execute("SELECT count(*) AS n FROM archive_gap").fetchone()["n"]
        assert gaps >= 1
        # basis and confirmation stay apart in every stored record
        assert all(f["confirmed_wall"] - f["basis_wall"] >= 0 for f in stored.values())
        assert min(f["confirmed_wall"] for f in stored.values() if f["point_id"] == "temp_wall_002") >= sc.at(127.0)[0] - 1
    finally:
        sc.gate.set()
        await sc.queue.stop(2.0)
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


def test_a_recovery_unit_with_1000_faults_and_the_full_baseline_stays_inside_the_group_budget():
    """I4, 终版 §12: the baseline grew from 200 to 222 points; a unit over the budget would be refused and the buffer never empty."""
    cfg = make_config()
    sink = []
    owner = Owner(cfg, WALL0, MONO0, commit=lambda g: sink.append(g), buckets=FixedBucket(), run_id="big")
    for source in ("mb_field", "ua_field"):
        owner.begin_generation(source, 1, WALL0, MONO0)
    items = tuple(r.Item(n, NOMINAL[n]) for n in UA_NAMES)
    owner.accept(r.InputBatch("ua_field", 1, r.KIND_UA_NOTIFY, 1001.0, 101.0, items, publish_seq=1))
    for accepted in sink:  # the writer has confirmed what it took (R03b: nothing older of the source is on its way)
        owner.committed(accepted)
    fault = r.FaultRecord(
        "x", "big", "ua_field", 1, 1, "temp_wall_001", "HIGH", 1.0, 1.0, "OBSERVATION", "big/ua_field/b1/i0", 1.0, 495.0,
        cfg.config_id, {"low": None, "high": 490.0, "hysteresis_low": 0.0, "hysteresis_high": 3.0},
    )  # fmt: skip
    from dataclasses import replace

    for n in range(1000):
        owner.fault_buffer[f"big/fault/{n}"] = replace(fault, id=f"big/fault/{n}")
    s = owner.sources["ua_field"]
    s.archive, s.lost_since_wall, s.lost_reason, s.lost_from_batch_seq = "LOST", 1001.0, "QUEUE_FULL", 1
    owner.tick(1003.0, 103.0)
    unit = sink[-1]
    size = group_bytes(unit)
    assert unit.kind == r.GROUP_RECOVERY and len(unit.faults) == 1000 and len(unit.observations) == 222
    assert size < cfg.storage.batch_budget_bytes, f"{size} bytes against {cfg.storage.batch_budget_bytes}"
    print(f"recovery unit: {len(unit.faults)} faults, {len(unit.observations)} observations, "
          f"{len(unit.results)} results = {size} bytes (budget {cfg.storage.batch_budget_bytes})")  # fmt: skip
