"""Crash recovery, storage switching and capacity, verified against the real system (方案 D47; 终版 §13.5 容量报告).

    python scripts/verify_resilience.py crash|storage|capacity|all --out docs/evidence/d47

crash     the backend runs as its own process on throw-away databases and is killed hard - during start-up, then at
          random moments under full load - and restarted; after every kill no commit group is half written, and
          after the restart every earlier run has been closed from what is persisted.
storage   the 95 % / 90 % thresholds are driven with INJECTED free-space readings through the live runtime (a real
          disk is never filled): which database each group lands in, hysteresis, no archive loss, history merged.
capacity  the measured growth of the full-scale input (all 208 inputs changing every second, and nothing changing), taken
          after the first start prepared its buckets: how often the rolling deletion comes, how many days are kept after
          it - less the write-ahead log and maintenance peaks - whether that holds the floor of buckets, and how long the
          emergency database lasts above its reserve.  There is no one-year retention to pass any more.

Everything is created and dropped here; the existing development and research databases are never touched.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.request import Request, urlopen
from uuid import uuid4

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
import measure  # noqa: E402

from htsg.auth import Accounts  # noqa: E402
from htsg.buckets import PREPARED_AHEAD  # noqa: E402
from htsg.simulation import FieldSimulator  # noqa: E402

GIB = 1024**3
# A row of `sample` names no group (修复轮14): the rows a database holds are counted against what its groups say they wrote.
# Rows a group said it wrote and that are not there are missing; rows no group accounts for are orphans.
ROWS_SQL = """
SELECT (SELECT coalesce(sum(records), 0) FROM commit_group),
       (SELECT count(*) FROM sample) + (SELECT count(*) FROM state_event)
"""


def fetch(url: str, credential: str = "", timeout=5):
    request = Request(url, headers={"Authorization": "Bearer " + credential} if credential else {})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


# ------------------------------------------------------------------------------------------------------ crash


class Backend:
    """The delivered entry point, `python -m htsg.cli run`, as a process of its own in a throw-away working folder."""

    def __init__(self, work: Path, config_path: Path, databases: Path, accounts: Path):
        self.work, self.port = work, measure.free_port()
        self.command = [
            sys.executable, "-m", "htsg.cli", "run", "--config", str(config_path), "--database", str(databases),
            "--accounts", str(accounts), "--port", str(self.port),
        ]  # fmt: skip
        self.proc = None

    def start(self):
        log = (self.work / f"backend-{uuid4().hex[:6]}.log").open("w")
        flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0  # so a Ctrl+Break reaches only this process
        self.proc = subprocess.Popen(self.command, cwd=self.work, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)

    async def until_ready(self, viewer: str, timeout=60):
        """The web application answers and the archive has written something."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            assert self.proc.poll() is None, f"the backend exited early with {self.proc.returncode}"
            try:
                health = await asyncio.to_thread(fetch, f"http://127.0.0.1:{self.port}/api/health", viewer)
                if health["archive"].get("queue", {}).get("committed", 0) > 0:
                    return health
            except OSError:
                pass
            await asyncio.sleep(0.3)
        raise AssertionError("the backend never wrote to the archive")

    def kill(self):
        self.proc.kill()  # TerminateProcess: no handler, no cleanup, no chance to flush
        self.proc.wait()

    async def stop(self, timeout=40):
        """A normal stop is Ctrl+Break to the process group (what uvicorn handles on Windows).  The wait is off the
        event loop: the simulated field that the backend is saying goodbye to is served by this very loop."""
        try:
            os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
            return await asyncio.to_thread(self.proc.wait, timeout)
        except subprocess.TimeoutExpired:
            return f"NOT_VERIFIED: still running {timeout} s after Ctrl+Break"
        except (OSError, AttributeError) as error:
            return f"NOT_VERIFIED: {type(error).__name__}"

    def ensure_gone(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()


def database_check(store) -> dict:
    with psycopg.connect(store.dsn) as conn:
        if conn.execute("SELECT to_regclass('commit_group')").fetchone()[0] is None:  # killed before the schema was even laid down
            return {"schema": False, "groups": 0, "missing_rows": 0, "orphan_rows": 0, "runs": [], "open_segments_in_ended_runs": 0}
        said, held = conn.execute(ROWS_SQL).fetchone()
        missing, orphans = max(0, said - held), max(0, held - said)
        groups = conn.execute("SELECT count(*) FROM commit_group").fetchone()[0]
        runs = conn.execute("SELECT run_id, ended_wall IS NOT NULL, end_reason FROM run ORDER BY started_wall").fetchall()
        open_segments = conn.execute(
            "SELECT count(*) FROM segment s JOIN run r USING(run_id) WHERE r.ended_wall IS NOT NULL AND s.end_wall IS NULL"
        ).fetchone()[0]
    return {"schema": True, "groups": groups, "missing_rows": missing, "orphan_rows": orphans, "runs": [(ended, reason) for _, ended, reason in runs], "open_segments_in_ended_runs": open_segments}


async def crash(iterations: int, seed: int = 7) -> dict:
    rng, report = random.Random(seed), {"kills": []}
    dbs = measure.Databases("htsg_crash_")
    work = Path(os.environ.get("TEMP", ".")) / ("htsg-crash-" + uuid4().hex)
    work.mkdir(parents=True)
    config = measure.make_config(stats_interval=60)
    primary, emergency = dbs.make("primary", config.timing), dbs.make("emergency", config.timing)
    (work / "config.json").write_text(json.dumps(config.model_dump(mode="json")), encoding="utf-8")
    (work / "database.json").write_text(json.dumps({"primary": primary.dsn, "emergency": emergency.dsn}), encoding="utf-8")
    accounts = Accounts(work / "accounts.json")
    viewer = accounts.create("viewer", "viewer")
    accounts.create("admin", "admin")
    field = FieldSimulator(config, write_ceiling=True)  # every input changing every second, as before 修复轮15
    await field.start()
    backends = []
    try:
        for n in range(iterations):
            backend = Backend(work, work / "config.json", work / "database.json", work / "accounts.json")
            backends.append(backend)
            backend.start()
            started = time.monotonic()
            if n == 0:
                await asyncio.sleep(0.5)  # before the run is registered: the process is still starting
                mode = "during start-up"
            else:
                await backend.until_ready(viewer)
                await asyncio.sleep(rng.uniform(0.2, 4.0))  # somewhere in the stream of commits, at full load
                mode = "under load"
            backend.kill()
            checks = {name: database_check(store) for name, store in (("primary", primary), ("emergency", emergency))}
            report["kills"].append({"mode": mode, "after_seconds": round(time.monotonic() - started, 2), **checks})
            for check in checks.values():
                assert check["missing_rows"] == 0 and check["orphan_rows"] == 0, check
        final = Backend(work, work / "config.json", work / "database.json", work / "accounts.json")
        backends.append(final)
        final.start()
        await final.until_ready(viewer)
        await asyncio.sleep(4)
        body = await asyncio.to_thread(fetch, f"http://127.0.0.1:{final.port}/api/replay?center={time.time() - 30}&seconds=7200&points=bridge_mvv_01,temp_inner_01", viewer, 30)
        report["history_after_crashes"] = {"channels": sorted(body["channels"]), "partial": body["partial"], "gaps": len(body["gaps"]), "rows": {k: sum(len(s["rows"]) for s in v["segments"]) for k, v in body["channels"].items()}}
        report["graceful_stop"] = await final.stop()
        report["after"] = {name: database_check(store) for name, store in (("primary", primary), ("emergency", emergency))}
        report["backend_logs"] = sorted(p.name for p in work.glob("backend-*.log"))
    finally:
        for backend in backends:
            backend.ensure_gone()
        await field.stop()
        dbs.drop()
        shutil.rmtree(work, ignore_errors=True)
    return report


# ----------------------------------------------------------------------------------------------------- storage


class InjectedDisks:
    """Free-space readings the test decides; the real drive is only ever read, never filled."""

    TOTAL = 1000 * GIB

    def __init__(self):
        self.used = {}

    def __call__(self, path):
        fraction = self.used.get(str(path).replace("\\", "/"), 0.10)
        return SimpleNamespace(total=self.TOTAL, used=int(self.TOTAL * fraction), free=int(self.TOTAL * (1 - fraction)))


def group_counts(lab) -> dict:
    out = {}
    for name, store in lab.router.stores:
        with psycopg.connect(store.dsn) as conn:
            out[name] = conn.execute("SELECT count(*) FROM commit_group").fetchone()[0]
    return out


async def storage(seconds: float) -> dict:
    report = {}
    drive = Path(measure.load_settings(measure.ROOT / "config/research-sim.json").storage.primary_path).drive + "/"
    free_before = shutil.disk_usage(drive).free
    steps = [("primary 40 %", {}, "PRIMARY"), ("primary 96 %", {"primary": 0.96}, "EMERGENCY"), ("primary 92 %: hysteresis holds", {"primary": 0.92}, "EMERGENCY"), ("primary 89 %", {"primary": 0.89}, "PRIMARY")]
    disks = InjectedDisks()
    async with measure.Lab(stats_interval=60, disk_usage=disks, prefix="htsg_switch_") as lab:
        storage_paths = {"primary": lab.config.storage.primary_path}
        await measure.initial_burst(lab)
        seen, rows = group_counts(lab), []
        for label, used, expected in steps:
            for kind, fraction in used.items():
                disks.used[storage_paths[kind]] = fraction
            await asyncio.sleep(seconds)
            now = group_counts(lab)
            landed = {name: now[name] - seen[name] for name in now}
            seen = now
            archive = {s: lab.runtime.owner.history_status(s)["archive"] for s in lab.runtime.owner.sources}
            rows.append({"step": label, "route": lab.router.mode, "expected": expected, "groups_landed": landed, "archive": archive})
        await measure.steady(lab, 1)  # settle: everything offered is durable
        whole = {}
        for name, store in lab.router.stores:
            check = database_check(store)
            whole[name] = {k: check[k] for k in ("missing_rows", "orphan_rows")}
        ids = []
        for _, store in lab.router.stores:
            with psycopg.connect(store.dsn) as conn:
                ids += [r[0] for r in conn.execute("SELECT group_id FROM commit_group").fetchall()]
        history = await asyncio.to_thread(fetch, f"http://127.0.0.1:{lab.port}/api/replay?center={time.time() - 10}&seconds=600&points=bridge_mvv_01", lab.viewer, 30)  # off the loop: the web application is served by it
        # the receipts: a restatement counts from the very start of its segment (修复轮14 keeps no origin per row)
        raw = [x for s in history["channels"]["bridge_mvv_01"]["segments"] for x in s["rows"] if x["time"] != s["start_wall"]]
        report["switching"] = {
            "steps": rows, "whole_groups": whole, "group_ids": len(ids), "group_ids_unique": len(set(ids)) == len(ids),
            "history": {"partial": history["partial"], "raw_rows": len(raw), "duplicates": len(raw) - len({(x["time"], x["value"]) for x in raw}), "in_order": [x["time"] for x in raw] == sorted(x["time"] for x in raw)},
            "owner_archive_lost_groups": lab.runtime.owner.health["archive_lost_groups"],
        }
    report["real_disk_growth_bytes"] = free_before - shutil.disk_usage(drive).free
    return report


# ---------------------------------------------------------------------------------------------------- capacity


def retention_capacity(bytes_per_second: float, *, wal_peak: int | None, maintenance_peak: int, primary_total: int,
                       emergency_free: int, reserve_bytes: int, cleanup_start: float, cleanup_stop: float,
                       floor_buckets: int) -> dict:  # fmt: skip
    """The capacity with the rolling deletion (终版 §13.5 容量报告): there is no year to hold any more.  From the measured daily
    growth (indexes inside it: the database size counts them once): how often a deletion comes - the room between the start
    and the stop line - and how many days are kept after one - the room below the stop line, less the write-ahead log peak
    and the maintenance peak once each.  The floor of buckets is held when those days are at least as many as the buckets
    (a bucket holds about a day).  The emergency database is not thinned: its days are its room above the reserve.  Other
    files on the disks, and the tables kept for good growing, are not counted here: the self-check page shows the real pace."""
    per_day = bytes_per_second * 86400
    room = cleanup_stop * primary_total - (wal_peak or 0) - maintenance_peak
    between = (cleanup_start - cleanup_stop) * primary_total / per_day if per_day else None
    kept = room / per_day if per_day else None
    held = kept is not None and kept >= floor_buckets
    return {
        "bytes_per_day": int(per_day), "primary_total_bytes": primary_total, "cleanup_start": cleanup_start, "cleanup_stop": cleanup_stop,
        "wal_peak_bytes": wal_peak, "maintenance_peak_bytes": maintenance_peak, "days_between_deletions": between,
        "days_kept_after_deletion": kept, "floor_buckets": floor_buckets,
        "emergency_free_bytes": emergency_free, "reserve_bytes": reserve_bytes,
        "emergency_days": max(0, emergency_free - reserve_bytes) / per_day if per_day else None,
        "verdict": "FLOOR_HELD" if held else "FLOOR_NOT_HELD: the space below the stop line keeps fewer days than the floor of buckets",
    }  # fmt: skip


PREPARE_WAIT = 60.0  # seconds the first start may take to prepare its buckets before the measurement is called invalid


def bucket_counts(lab) -> dict:
    """Buckets each database of the lab has, by state."""
    out = {}
    for name, store in lab.router.stores:
        with psycopg.connect(store.dsn) as conn:
            out[name] = dict(conn.execute("SELECT state,count(*) FROM bucket GROUP BY state ORDER BY state").fetchall())
    return out


async def first_preparation(lab, deadline: float) -> dict | None:
    """Wait until the first start has prepared its buckets: every database holds the ready ones it keeps in hand.  That
    preparation happens once; inside the window it would be taken for growth and multiplied by a year."""
    while True:
        counts = await asyncio.to_thread(bucket_counts, lab)
        if all(c.get("READY", 0) >= PREPARED_AHEAD for c in counts.values()):
            return counts
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.5)


async def capacity(seconds: float) -> dict:
    report = {}
    config = measure.load_settings(measure.ROOT / "config/research-sim.json")
    policy = config.storage
    drive = Path(policy.primary_path).drive + "/"
    usage = shutil.disk_usage(drive)
    emergency = shutil.disk_usage(Path(policy.emergency_path).drive + "/")
    for name, quiet in (("every_input_changes_every_second", False), ("no_input_changes", True)):
        async with measure.Lab(stats_interval=5, quiet=quiet, prefix="htsg_cap_") as lab:
            await measure.initial_burst(lab)
            waited = time.monotonic()
            at_start = await first_preparation(lab, waited + PREPARE_WAIT)
            if at_start is None:
                report[name] = {"valid": False, "reason": f"the first bucket preparation did not finish in {PREPARE_WAIT:.0f} s"}
                continue
            waited = time.monotonic() - waited
            await asyncio.sleep(2)
            before, since = lab.counters(), lab.samples[-1]["t"]
            await asyncio.sleep(seconds)
            await lab.settled()
            part = measure.load_report(lab, before, since)
            at_end = await asyncio.to_thread(bucket_counts, lab)
            tables = await asyncio.to_thread(lab.table_sizes)
            total = sum(t["data"] + t["indexes"] for t in tables.values())
            size_now = await asyncio.to_thread(lambda: lab.storage_sizes()[0])
            rate = part["database_bytes_per_second"]
            swing = max(0, part["database_bytes_peak"] - size_now)
            report[name] = {
                "valid": True, "buckets_at_start": at_start, "buckets_at_end": at_end,
                "first_preparation": {"waited_seconds": round(waited, 1), "database_bytes_at_window_start": before["database_bytes"]},
                "measured_seconds": part["seconds"], "rows_per_second": part["rows_per_second"], "database_bytes_per_second": rate,
                "bytes_per_row": round(rate / part["rows_per_second"], 1) if part["rows_per_second"] else None,
                "tables": tables, "tables_total_bytes": total, "database_size_bytes": size_now,
                "capacity": retention_capacity(
                    rate, wal_peak=part["wal_bytes_peak"], maintenance_peak=swing, primary_total=usage.total,
                    emergency_free=emergency.free, reserve_bytes=policy.emergency_reserve_bytes, cleanup_start=policy.cleanup_start,
                    cleanup_stop=policy.cleanup_stop, floor_buckets=policy.retention_min_buckets,
                ),  # fmt: skip
            }
    report["volume"] = {"drive": drive, "total_bytes": usage.total, "free_bytes": usage.free}
    report["not_proven"] = ("simulated field only: neither the real plant's change rate nor the real disk is evidence for the deletion "
                            "period or the days kept after a deletion")
    return report


async def main(args):
    report = {"started": time.strftime("%Y-%m-%d %H:%M:%S")}
    if args.part in ("crash", "all"):
        report["crash"] = await crash(args.kills)
    if args.part in ("storage", "all"):
        report["storage"] = await storage(args.phase_seconds)
    if args.part in ("capacity", "all"):
        report["capacity"] = await capacity(args.capacity_seconds)
    report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    print(text)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"resilience-{args.part}-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(text, encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("part", choices=("crash", "storage", "capacity", "all"))
    parser.add_argument("--kills", type=int, default=6, help="processes killed in the crash part (the first during start-up)")
    parser.add_argument("--phase-seconds", type=float, default=6, help="seconds per injected disk reading")
    parser.add_argument("--capacity-seconds", type=float, default=120, help="seconds of growth measured per input behaviour")
    parser.add_argument("--out", default="")
    asyncio.run(main(parser.parse_args()))
