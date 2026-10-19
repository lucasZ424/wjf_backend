"""Full-scale protocol and performance measurement (方案 D46).

Real Modbus and OPC UA servers (the independent research simulator: 8 bridges + 200 monitored items), the real
runtime with its owner, ordered ingress, commit queue and archive router, throw-away PostgreSQL databases on the
development clusters (created here, dropped here, nothing else is touched) and, with --browser, a real Chrome on
page 4 that applies and draws every pushed group.  It reports what happened; it does not decide whether that is
enough (终版 §16: measure, then compare with the requirement).

    python scripts/measure.py --steady 120 --outage 25 --browser --out docs/evidence/d46
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import json
import os
import socket
import statistics
import sys
import time
from pathlib import Path
from uuid import uuid4

import psycopg
import uvicorn
from psycopg import sql
from psycopg.conninfo import make_conninfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from htsg.archive import ArchiveStore  # noqa: E402
from htsg.archive_router import ArchiveRouter  # noqa: E402
from htsg.auth import Accounts  # noqa: E402
from htsg.config import Settings, load_settings  # noqa: E402
from htsg.runtime import Runtime  # noqa: E402
from htsg.simulation import FieldSimulator  # noqa: E402
from htsg.web import create_web_app  # noqa: E402


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def rss_bytes() -> int:
    """Resident memory of this process (Windows working set); 0 where it cannot be read."""

    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in ("Peak", "WorkingSetSize", "QPP", "QP", "QNPP", "QNP", "Pagefile", "PeakPagefile")
        ]  # fmt: skip

    try:
        from ctypes import wintypes

        kernel, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return 0
        return int(counters.WorkingSetSize)
    except (AttributeError, OSError, ImportError):
        return 0


def spread(values) -> dict:
    """count / min / median / 95th / 99th / max, in the unit of the input."""
    values = sorted(values)
    if not values:
        return {"count": 0}
    at = lambda q: values[min(len(values) - 1, int(q * len(values)))]  # noqa: E731
    return {"count": len(values), "min": values[0], "p50": statistics.median(values), "p95": at(0.95), "p99": at(0.99), "max": values[-1]}


def make_config(stats_interval=5.0, outage=0.0, quiet=False, external=False) -> Settings:
    """The research configuration on free local ports.  `outage` sets how long the disconnect scenarios last; `quiet`
    holds every simulated value still; `external` enables a removable-disk database."""
    data = load_settings(ROOT / "config/research-sim.json").model_dump()
    data["sources"][0]["port"] = free_port()
    data["sources"][1]["endpoint"] = f"opc.tcp://127.0.0.1:{free_port()}/measure/"
    data["simulation"]["control_port"] = free_port()
    # FieldSimulator corrects the locked asyncua server per instance, so measure the normal contract default.
    data["timing"] = {"stats_interval": stats_interval}
    for scenario in data["simulation"]["scenarios"]:
        if outage and scenario["kind"].startswith("disconnect_"):
            scenario["duration"] = outage
    if quiet:
        for signal in data["simulation"]["signals"]:
            signal["amplitude"], signal["noise"] = 0, 0
    if external:
        data["storage"].update(external_enabled=True, external_path="D:/HTSG-dev/external")
    return Settings.model_validate(data)


class Databases:
    """Throw-away databases on the development clusters.  The prefix says whose they are; they are dropped on exit
    and nothing else is ever touched (the existing development and research databases stay as they are)."""

    def __init__(self, prefix="htsg_perf_"):
        self.prefix, self.created = prefix, []
        self.credentials = json.loads((ROOT / ".local/database.json").read_text())

    def make(self, instance, timing=None) -> ArchiveStore:
        name = self.prefix + uuid4().hex
        admin = make_conninfo(self.credentials[instance], dbname="postgres")
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        self.created.append((admin, name))
        return ArchiveStore(make_conninfo(self.credentials[instance], dbname=name), timing)

    def drop(self):
        for admin, name in self.created:
            assert name.startswith(self.prefix) and len(name) == len(self.prefix) + 32
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


# Page side of the latency measurement: arrival of every pushed message, and the end of the first animation frame
# after it (the page draws once per frame).  The application itself is not touched.
PAGE_PROBE = """
(() => {
  const log = window.__push = [], waiting = [], Native = window.WebSocket;
  window.WebSocket = class extends Native {
    constructor(...args) {
      super(...args);
      this.addEventListener("message", event => {
        const found = typeof event.data === "string" && event.data.match(/^\\{"type":"groups","first":(\\d+),"last":(\\d+)/);
        if (found) { const rec = {first: +found[1], last: +found[2], arrived: performance.timeOrigin + performance.now(), drawn: null}; log.push(rec); waiting.push(rec); }
      });
    }
  };
  const raf = window.requestAnimationFrame.bind(window);
  window.requestAnimationFrame = callback => raf(stamp => {
    const started = performance.timeOrigin + performance.now();
    callback(stamp);
    const ended = performance.timeOrigin + performance.now();
    while (waiting.length && waiting[0].arrived <= started) waiting.shift().drawn = ended;
  });
})();
"""


class Lab:
    """One complete system on throw-away databases; `async with` builds it and tears it down."""

    def __init__(self, stats_interval=5.0, browser=False, outage=0.0, quiet=False, disk_usage=None, external=False, prefix="htsg_perf_"):
        """`quiet`: the simulated plant holds every value still.  `disk_usage`: injected free-space readings.
        `external`: a third (removable-disk) database is bound and enabled."""
        self.stats_interval, self.want_browser, self.outage, self.quiet = stats_interval, browser, outage, quiet
        self.disk_usage, self.external, self.databases = disk_usage, external, Databases(prefix)
        self.rows_written = 0
        self.samples = []
        self.commit_wait, self.offered, self.sent = [], {}, {}
        self.receipts = {}

    async def __aenter__(self):
        self.config = make_config(self.stats_interval, self.outage, self.quiet, self.external)
        self.field = FieldSimulator(self.config)
        await self.field.start()
        stores = [self.databases.make(i, self.config.timing) for i in ("primary", "emergency", "primary")[: 3 if self.external else 2]]
        extra = {"disk_usage": self.disk_usage} if self.disk_usage else {}
        self.router = ArchiveRouter(self.config, *stores, **extra)
        self.work = Path(os.environ.get("TEMP", ".")) / ("htsg-measure-" + uuid4().hex)
        accounts = Accounts(self.work / "accounts.json")
        self.viewer = accounts.create("viewer", "viewer")
        self.runtime = Runtime(self.config, self.router, accounts)
        self.instrument()
        app = create_web_app(
            config=self.config, accounts=accounts, publisher=self.runtime.publisher, archive_router=self.router,
            queue=self.runtime.queue, diagnostics=self.runtime.diagnostics, lifecycle=self.runtime,
        )  # fmt: skip
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.started, self.before_start = time.monotonic(), self.counters()
        self.serving = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.02)
        self.sampler = asyncio.create_task(self.sample())
        if self.want_browser:
            await self.open_browser()
        return self

    def instrument(self):
        """Timestamps taken from outside the product code: when a group was offered, sent and became durable."""
        owner, queue, publisher = self.runtime.owner, self.runtime.queue, self.runtime.publisher
        offer, committed, send, publish = owner.commit, queue.on_committed, publisher._send, publisher.offer

        def timed_offer(group):
            self.offered[group.group_id] = time.monotonic()
            return offer(group)

        def timed_committed(group, result):
            self.commit_wait.append(time.monotonic() - self.offered.pop(group.group_id, time.monotonic()))
            self.rows_written += group.record_count()
            return committed(group, result)

        def timed_publish(group):
            stamps = [o.received_wall for o in group.observations if o.received_wall]
            self.receipts[group.pub_seq] = (min(stamps) if stamps else None, group.wall, time.time())
            return publish(group)

        def timed_send(message):
            if message["type"] == "groups":
                self.sent[message["first"]] = time.time()
            return send(message)

        owner.commit, queue.on_committed, publisher.offer, publisher._send = timed_offer, timed_committed, timed_publish, timed_send

    async def sample(self):
        """Every 100 ms: queue depths and how late the event loop woke up; every second: memory."""
        last_memory = last_storage = 0.0
        expected = time.monotonic() + 0.1
        while True:
            await asyncio.sleep(0.1)
            now = time.monotonic()
            self.samples.append({
                "t": now - self.started, "lag": max(0.0, now - expected), "queue": self.runtime.queue.depth,
                "ingress": len(self.runtime.ingress.queue), "rss": rss_bytes() if now - last_memory >= 1 else None,
                "db": None, "wal": None,
            })  # fmt: skip
            last_memory = now if self.samples[-1]["rss"] is not None else last_memory
            if now - last_storage >= 5:  # database and write-ahead log size: a read of the files, off the event loop
                last_storage = now
                self.samples[-1]["db"], self.samples[-1]["wal"] = await asyncio.to_thread(self.storage_sizes)
            expected = time.monotonic() + 0.1

    async def open_browser(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.chrome = await self.playwright.chromium.launch(channel="chrome", headless=True)
        self.page = await self.chrome.new_page(viewport={"width": 1440, "height": 1000})
        await self.page.add_init_script(PAGE_PROBE)
        await self.page.goto(f"http://127.0.0.1:{self.port}/")
        await self.page.locator("#credential").fill(self.viewer)
        await self.page.locator("#login-form button").click()
        await self.page.get_by_role("button", name="热应力分析").click()

    async def __aexit__(self, *exc):
        if self.want_browser:
            await self.chrome.close()
            await self.playwright.stop()
        self.sampler.cancel()
        self.server.should_exit = True
        await self.serving  # the lifespan stops the runtime: protocols, the end of the run, the archive's backlog
        await self.field.stop()
        self.databases.drop()

    # ----------------------------------------------------------------------------------------- readings

    def views(self):
        return self.runtime.owner.channel_views()

    async def wait(self, condition, timeout: float, why: str):
        """Wait for something to be true - and say what, and where the simulated field stood, if it never is."""
        started = time.monotonic()
        while not condition():
            if time.monotonic() - started > timeout:
                field = self.field.status()
                raise TimeoutError(f"{why}: not after {timeout:.0f} s (field task done: {self.field.task.done()}, scenario {field['scenario']}, servers {field['servers']}, error {field['error']})")
            await asyncio.sleep(0.05)

    def everything_valid(self, only=None):
        views = self.views()
        return all(v["valid"] for pid, v in views.items() if only is None or pid in only)

    def database_bytes(self):
        with psycopg.connect(self.router.primary.dsn) as conn:
            return conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]

    def storage_sizes(self):
        """(business database bytes including indexes, write-ahead log bytes of the cluster or None if unreadable)."""
        with psycopg.connect(self.router.primary.dsn) as conn:
            size = conn.execute("SELECT pg_database_size(current_database())").fetchone()[0]
            try:
                wal = int(conn.execute("SELECT coalesce(sum(size),0) FROM pg_ls_waldir()").fetchone()[0])  # numeric -> int
            except psycopg.Error:
                wal = None
        return size, wal

    def table_sizes(self):
        """Bytes per table with and without its indexes (the total of both is what the database size contains)."""
        with psycopg.connect(self.router.primary.dsn) as conn:
            rows = conn.execute(
                "SELECT relname, pg_table_size(oid) AS data, pg_indexes_size(oid) AS indexes FROM pg_class "
                "WHERE relkind='r' AND relnamespace='public'::regnamespace ORDER BY pg_total_relation_size(oid) DESC"
            ).fetchall()
            counts = {name: conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(name))).fetchone()[0] for name, _, _ in rows}
        return {name: {"data": int(data), "indexes": int(indexes), "rows": counts[name]} for name, data, indexes in rows}

    def counters(self):
        owner, queue = self.runtime.owner, self.runtime.queue
        return {
            "groups_committed": queue.stats["committed"], "groups_offered": queue.stats["offered"],
            "rows_committed": getattr(self, "rows_written", 0), "bytes_estimated": self.router.bytes_written,
            "database_bytes": self.database_bytes(), "published": owner.pub_seq, "order": owner.order,
            "ingress_dropped": self.runtime.ingress.dropped, "queue_rejected": queue.stats["rejected"],
            "refused": queue.stats["refused"], "failures": queue.stats["failures"] + queue.stats["unknown"],
            "resyncs": owner.health["resyncs"], "time": time.monotonic(),
        }  # fmt: skip

    def window(self, since: float):
        return [s for s in self.samples if s["t"] >= since]

    async def settled(self, timeout=30.0):
        """Wait until every offered group is durable; the time it took is the backlog the load left."""
        started = time.monotonic()
        while self.runtime.queue.depth or self.runtime.queue.stats["highest_committed_order"] < self.runtime.owner.order:
            if time.monotonic() - started > timeout:
                return None
            await asyncio.sleep(0.05)
        return time.monotonic() - started


def load_report(lab: Lab, before: dict, since: float) -> dict:
    after = lab.counters()
    seconds = after["time"] - before["time"]
    samples = lab.window(since)
    memory = [s["rss"] for s in samples if s["rss"]]
    rate = lambda key: (after[key] - before[key]) / seconds  # noqa: E731
    return {
        "seconds": round(seconds, 1),
        "groups_per_second": round(rate("groups_committed"), 2), "rows_per_second": round(rate("rows_committed"), 1),
        "database_bytes_per_second": round(rate("database_bytes")), "estimated_bytes_per_second": round(rate("bytes_estimated")),
        "published_groups": after["published"] - before["published"], "committed_groups": after["groups_committed"] - before["groups_committed"],
        "unexplained": {k: after[k] - before[k] for k in ("ingress_dropped", "queue_rejected", "refused", "failures")},
        "queue_depth": spread([s["queue"] for s in samples]), "ingress_depth": spread([s["ingress"] for s in samples]),
        "event_loop_lateness_ms": {k: round(v * 1000, 1) for k, v in spread([s["lag"] for s in samples]).items() if k != "count"},
        "commit_wait_ms": {k: round(v * 1000, 1) for k, v in spread(lab.commit_wait).items() if k != "count"},
        "memory_mb": {"start": round(memory[0] / 1e6, 1), "end": round(memory[-1] / 1e6, 1), "max": round(max(memory) / 1e6, 1)} if memory else None,
        "maintenance_max_seconds": round(lab.runtime.health["maintenance_max_seconds"], 3),
        "database_bytes_peak": max([s["db"] for s in samples if s["db"]] or [0]),
        "wal_bytes_peak": max([s["wal"] for s in samples if s["wal"] is not None] or [None], key=lambda x: -1 if x is None else x),
    }  # fmt: skip


async def initial_burst(lab: Lab) -> dict:
    """Everything arrives at once: the first notification of 200 items, the first Modbus rounds.  Timed from the
    moment the system was started, so connecting, subscribing and the opening of the archive are in it."""
    before, started = lab.before_start, lab.started
    await lab.wait(lab.everything_valid, 60, "the 417 channels never became valid")
    valid_after = time.monotonic() - started
    backlog = await lab.settled()
    return {"all_417_valid_after_seconds": round(valid_after, 2), "archive_caught_up_after_seconds": None if backlog is None else round(backlog, 2), **load_report(lab, before, 0.0)}


async def steady(lab: Lab, seconds: float) -> dict:
    before, since = lab.counters(), lab.samples[-1]["t"] if lab.samples else 0.0
    lab.commit_wait.clear()
    await asyncio.sleep(seconds)
    backlog = await lab.settled()
    return {"backlog_left_after_seconds": None if backlog is None else round(backlog, 2), **load_report(lab, before, since)}


async def outage(lab: Lab, scenario: str, source: str, seconds: float) -> dict:
    """The protocol side disappears for `seconds` and returns: how long until the source is seen as down, until it is whole again."""
    before, since = lab.counters(), lab.samples[-1]["t"]
    inputs = {p.id for p in lab.config.points if p.source == source}
    lab.field.set_scenario(scenario)
    cut = time.monotonic()
    await lab.wait(lambda: not lab.everything_valid(inputs), seconds + 30, f"the {source} outage was never noticed")
    noticed = time.monotonic() - cut
    await lab.wait(lambda: lab.field.scenario.kind == "baseline", seconds + 30, "the scenario ends by itself after its duration")
    back = time.monotonic()
    await lab.wait(lab.everything_valid, 120, f"{source} never became valid again")
    backlog = await lab.settled()
    return {
        "source": source, "outage_seconds": round(back - cut, 1), "noticed_after_seconds": round(noticed, 2),
        "valid_again_after_seconds": round(time.monotonic() - back, 2), "archive_backlog_cleared_after_seconds": None if backlog is None else round(backlog, 2),
        "resyncs": lab.counters()["resyncs"] - before["resyncs"], "archive_states": {s: lab.runtime.owner.history_status(s)["archive"] for s in lab.runtime.owner.sources},
        "unexplained": load_report(lab, before, since)["unexplained"],
    }  # fmt: skip


async def push_latency(lab: Lab) -> dict:
    """From the backend's receipt of a group's input to the page having applied and drawn it, in parts."""
    records = await lab.page.evaluate("window.__push")
    parts = {"within_the_backend": [], "coalescing_wait": [], "send_to_drawn": [], "receipt_to_drawn": []}
    for message in records:
        sent = lab.sent.get(message["first"])
        if sent is None or message["drawn"] is None:
            continue
        for seq in range(message["first"], message["last"] + 1):
            receipt, built, offered = lab.receipts.get(seq, (None, None, None))
            if receipt is None:
                continue
            drawn = message["drawn"] / 1000
            parts["within_the_backend"].append(offered - receipt)  # the round's own wait and the assembly of its group
            parts["coalescing_wait"].append(sent - offered)  # the up-to-100-ms packing, reported apart
            parts["send_to_drawn"].append(drawn - sent)
            parts["receipt_to_drawn"].append(drawn - receipt)
    return {name: {k: round(v * 1000, 1) for k, v in spread(values).items() if k != "count"} | {"groups": len(values)} for name, values in parts.items()}


async def main(args):
    report = {
        "started": time.strftime("%Y-%m-%d %H:%M:%S"), "host": socket.gethostname(),
        "python": sys.version.split()[0], "quiet": args.quiet,
    }
    async with Lab(stats_interval=args.stats_interval, browser=args.browser, outage=args.outage, quiet=args.quiet) as lab:
        report["initial_burst"] = await initial_burst(lab)
        report["steady"] = await steady(lab, args.steady)
        if args.outage:
            report["outages"] = [await outage(lab, "disconnect_opcua", "ua_field", args.outage), await outage(lab, "disconnect_modbus", "mb_field", args.outage)]
            await steady(lab, 5)  # the system is running normally again before the end
        if args.browser:
            report["push_latency_ms"] = await push_latency(lab)
        report["owner_health"] = dict(lab.runtime.owner.health)
        report["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    report["stop"] = {
        key: lab.runtime.health[key]
        for key in ("shutdown_incomplete", "run_end_status", "failed_sources", "unsettled_queue_items")
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"measure-{time.strftime('%Y%m%d-%H%M%S')}.json").write_text(text, encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--steady", type=float, default=60, help="seconds of steady full-scale input")
    parser.add_argument("--outage", type=float, default=0, help="seconds each protocol is cut off (0: skip)")
    parser.add_argument("--stats-interval", type=float, default=5, help="seconds between the heavy database statistics")
    parser.add_argument("--browser", action="store_true", help="measure push-to-drawn latency in a real Chrome")
    parser.add_argument("--quiet", action="store_true", help="hold every simulated input constant")
    parser.add_argument("--out", default="", help="folder for the JSON report")
    asyncio.run(main(parser.parse_args()))
