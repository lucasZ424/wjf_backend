"""Make ONE abnormal situation on real protocol servers and print how the real backend answers it, as a timeline.

    python scripts/observe_response.py --list                      the situations and their settings
    python scripts/observe_response.py                             run the situation named in .local/observe/observe.json
    python scripts/observe_response.py --config FILE --out DIR     ... another file; DIR receives timeline.txt and evidence.json

The configuration file is JSON, e.g.   {"situation": "modbus_delay", "start": 10, "duration": 4, "after": 20, "delay": 1.5}
  start     seconds after every input was first valid, when the situation is made
  duration  seconds it lasts (then it is taken away again)
  after     seconds still watched after the situation is over (recovery)
  watch     optional list of points to show; the other settings belong to the situation (see --list)
  web_port  optional: also serve the real web pages on this port (login token admin:admin) while the run lasts
One run is one situation; there is no sequence.  The backend is the real `Runtime` (owner, ordered ingress, both acquirers,
commit queue, archive router, web application) on throw-away databases on the development clusters (created here, dropped here,
nothing else is touched - start them first: scripts/bootstrap_db.py start).  The servers are the project's protocol test servers
(tests/protocol_support.py), not the research simulator.  Nothing in htsg/ is changed; this tool is not part of the candidate.

The timeline shows a line whenever something changes (source state, alarm, generation, resync and connect-failure counts, validity of
the watched points, fault state) and a heartbeat line every few seconds, plus the communication-log entries as they appear and grow.
Times are seconds from the moment every input was first valid; a marker line says when the situation was SET and, where the server can
tell, when it first TOOK EFFECT (the first request it held back, the first value it wrote, ...).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import sys
import tempfile
import time
from copy import copy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests"), str(ROOT / "scripts")]

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from live_support import PROCESS, UA_NAMES, full_config  # noqa: E402
from measure import Databases  # noqa: E402
from protocol_support import (  # noqa: E402
    FieldModbus,
    FieldModbusDevices,
    FieldUa,
    PausableProxy,
    device_points,
)

from htsg.archive_router import ArchiveRouter  # noqa: E402
from htsg.auth import Accounts  # noqa: E402
from htsg.config import Settings, Timing  # noqa: E402
from htsg.runtime import Runtime  # noqa: E402
from htsg.web import create_web_app  # noqa: E402

logging.getLogger("asyncua").setLevel(logging.ERROR)
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)  # the libraries' own chatter would bury the timeline; the backend's facts come from its log
DEFAULT_CONFIG = ROOT / ".local/observe/observe.json"
FULL_WATCH = ["bridge_mvv_01", "strain_01", "strain_change_01", "temp_inner_01", "delta_t_01"]
SLAVES_WATCH = ["d1_a", "d1_b", "d2_a", "d2_b"]
STATUS_OVERFLOW = 0x0480  # info type "data value" + info bit "Overflow" (OPC UA part 4, 7.34)

SITUATIONS = {
    "modbus_delay": "every Modbus request is held `delay` s (default 1.5; above the 0.8 s request timeout it times out; the late reply then answers the next request, so this is ONE timeout)  [row 1]",
    "modbus_silent": "the Modbus device never answers (requests are held until the server stops); a constant `delay` cannot do this, the fixture lets a late reply answer the next request  [rows 2, 11]",
    "loop_stall": "the whole process blocks for `duration` s (a scheduler stall); planned Modbus rounds are skipped, none replayed  [row 5; servers stall too]",
    "modbus_exception": "the Modbus device answers every request with a protocol exception  [row 3, one device]",
    "modbus_slaves": "two slaves on one server, `modes` {\"1\": \"OK\", \"2\": \"SILENT\" | \"EXCEPTION\"} set for the duration  [row 3, partial answers]",
    "ua_silence": "the OPC UA connection stays open but the traffic stops (pausable proxy)  [row 6]",
    "ua_write": "`writes` [{\"at\": s after start, \"point\": id, \"value\": x, \"good\": true}]; `restore` true writes the old values back at the end. "
    "No writes = a quiet source  [rows 7, 8, 9 (same-batch versions: two writes less than a second apart)]",
    "ua_gap": "the OPC UA server skips `skip` (default 1) notification sequence numbers at `start`  [row 9, gap]",
    "ua_overflow": "`burst` (default 15) quick writes to `point` overflow the item queue; the server marks the survivor Overflow  [row 9, overflow]",
    "ua_server_params": "the OPC UA server revises the parameters before the backend connects: `keepalive_cap`/`lifetime_cap`/`queue_cap` lower them, `keepalive_floor` raises the keep-alive count  [row 10]",
}


def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=False).stdout.strip()


class Observation:
    """The servers, the real runtime on throw-away databases, and the timeline."""

    def __init__(self, cfg: dict, out: Path | None):
        self.cfg, self.out = cfg, out
        self.t0 = None
        self.lines, self.transitions, self.markers = [], [], []
        self.armed = None  # the injection waiting for the first thing it affects
        self.requests = 0
        self.log_seen = {}

    # ------------------------------------------------------------------ output
    def now(self) -> float:
        return 0.0 if self.t0 is None else time.monotonic() - self.t0

    def say(self, text: str):
        line = f"{self.now():+8.2f}  {text}"
        self.lines.append(line)
        print(line, flush=True)

    def mark(self, what: str, **extra):
        self.markers.append({"t": round(self.now(), 3), "wall": time.time(), "what": what, **extra})
        self.say(f"##### {what}")

    # ------------------------------------------------------------------ servers
    def tap_modbus(self, server):
        """Note the first request the situation touches.  Installed before the server starts (it hands over `action`)."""
        if isinstance(server, FieldModbus):
            inner = server.action

            async def action(*args):
                self.requests += 1
                self.took_effect()
                return await inner(*args)

            server.action = action
        else:
            make = server._action_of

            def action_of(device):
                inner = make(device)

                async def action(*args):
                    self.requests += 1
                    if server.mode[device] != server.OK:
                        self.took_effect(f"device {device}")
                    return await inner(*args)

                return action

            server._action_of = action_of

    def took_effect(self, where=""):
        if self.armed:
            self.mark(f"TOOK EFFECT: {self.armed} {where} - first request it touched", effective=True)
            self.armed = None

    async def start(self):
        cfg, case = self.cfg, self.cfg["situation"]
        self.timing = Timing()
        self.databases = Databases("htsg_obs_")
        self.proxy = self.ua = self.mb = None
        if case == "modbus_slaves":
            self.mb = FieldModbusDevices((1, 2))
            self.tap_modbus(self.mb)
            await self.mb.start()
            points = device_points((1, 2))
            self.config = Settings.model_validate({
                "mode": "development", "dev_admin_login": True,
                "sources": [self.mb.source().model_dump()], "points": [p.model_dump() for p in points],
                "timing": self.timing.model_dump(),
            })  # fmt: skip
            self.watch = cfg.get("watch", SLAVES_WATCH)
        else:
            self.mb, self.ua = FieldModbus(), FieldUa(UA_NAMES)
            self.tap_modbus(self.mb)
            await self.mb.start()
            await self.ua.start()
            self.nominal = {}
            for i, name in enumerate(UA_NAMES):
                value = 480.0 + i * 0.01 if name.startswith("temp_inner") else 440.0 + i * 0.01 if name.startswith("temp_outer") else PROCESS[name]
                self.nominal[name] = value
                await self.ua.set(name, value)
            iserver = self.ua.server.iserver
            if case == "ua_server_params":  # a server that revises what the client asked for
                for key, attribute in (("keepalive_cap", "max_keep_alive_count"), ("lifetime_cap", "max_lifetime_count"), ("queue_cap", "max_monitored_item_queue_size")):
                    if key in cfg:
                        setattr(iserver, attribute, cfg[key])
            if "keepalive_floor" in cfg:  # the library only ever lowers a count; this server also RAISES it
                service, clamp, floor = iserver.subscription_service, iserver.subscription_service._clamp_keep_alive_count, int(cfg["keepalive_floor"])
                service._clamp_keep_alive_count = lambda requested: max(clamp(requested), floor)
            self.mark_overflow_on_drop(iserver.subscription_service)
            endpoint = self.ua.endpoint
            if case == "ua_silence":
                self.proxy = PausableProxy(self.ua.port)
                await self.proxy.start()
                endpoint = f"opc.tcp://127.0.0.1:{self.proxy.port}/t/"
            self.config = full_config(self.mb.port, endpoint, self.timing)
            self.watch = cfg.get("watch", FULL_WATCH)
        stores = [self.databases.make(i, self.config.timing) for i in ("primary", "emergency")]
        self.router = ArchiveRouter(self.config, *stores)
        self.work = tempfile.TemporaryDirectory(prefix="htsg-observe-")
        accounts = Accounts(Path(self.work.name) / "accounts.json")
        self.viewer = accounts.create("viewer", "viewer")
        accounts.create("admin", "admin")  # the development login (admin:admin) needs an active admin account
        self.runtime = Runtime(self.config, self.router, accounts, number_file=Path(self.work.name) / "bucket_number.json")
        self.rounds = []  # (seconds on the timeline, planned tick number) of every Modbus round the owner committed
        offer = self.runtime.owner.commit

        def noting_offer(group):
            for state in group.states:
                if state.kind == "POLL" and state.detail and "tick" in state.detail:
                    self.rounds.append((round(self.now(), 2), state.detail["tick"]))
            return offer(group)

        self.runtime.owner.commit = noting_offer
        self.app = create_web_app(
            config=self.config, accounts=accounts, owner=self.runtime.owner, archive_router=self.router,
            queue=self.runtime.queue, diagnostics=self.runtime.diagnostics, lifecycle=self.runtime,
        )  # fmt: skip
        self.web = None
        if cfg.get("web_port"):  # the web application starts and stops the runtime itself (its lifespan): never twice
            self.web = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=cfg["web_port"], log_level="warning"))
            self.serving = asyncio.create_task(self.web.serve())
            while not self.web.started:
                await asyncio.sleep(0.05)
        else:
            await self.runtime.start()

    @staticmethod
    def mark_overflow_on_drop(service):
        """The asyncua server drops the oldest queued value silently.  This marks the survivor Overflow, as the standard says a
        server must: one patched service INSTANCE (like the project's keep-alive adapter), never the library."""
        from types import MethodType

        from asyncua import ua

        create = service.create_subscription

        async def create_subscription(self, params, callback, session_id, request_callback=None):
            result = await create(params, callback, session_id, request_callback)
            subscription = self.subscriptions[result.SubscriptionId]
            enqueue = subscription._enqueue_event

            async def _enqueue_event(instance, mid, eventdata, size, queue):
                full = mid in queue and size != 0 and len(queue[mid]) >= size
                await enqueue(mid, eventdata, size, queue)
                if full and hasattr(eventdata, "Value"):
                    value = copy(eventdata.Value)  # the node's own DataValue is the same object: marking it would stay on the node
                    value.StatusCode = ua.StatusCode((value.StatusCode.value if value.StatusCode else 0) | STATUS_OVERFLOW)
                    eventdata.Value = value

            subscription._enqueue_event = MethodType(_enqueue_event, subscription)
            return result

        service.create_subscription = MethodType(create_subscription, service)

    async def stop(self):
        try:
            if self.web:
                self.web.should_exit = True
                await self.serving  # its lifespan stops the runtime
            else:
                await self.runtime.stop()
        finally:
            for server in (self.proxy, self.ua, self.mb):
                if server:
                    await server.stop()
            self.router.close()
            self.databases.drop()
            self.work.cleanup()

    # ------------------------------------------------------------------ what is seen
    def all_valid(self) -> bool:
        views = self.runtime.owner.channel_views()
        return bool(views) and all(v["valid"] for v in views.values())

    def picture(self) -> tuple[str, str]:
        """(changing part, display part).  The first decides whether a line is printed; values only decorate it."""
        owner = self.runtime.owner
        status, failures, views = owner.status(), self.runtime.connect_failures(), owner.channel_views()
        parts = []
        for sid, s in status["sources"].items():
            bad = sum(1 for p, source in owner.point_source.items() if source == sid and not views[p]["valid"])
            total = sum(1 for source in owner.point_source.values() if source == sid)
            parts.append(f"{sid}={s['state']} g{s['conn_gen']} seg{s['seg']} resync{s['resync_count']} cf{failures.get(sid, 0)}{' ALARM' if s['alarm'] else ''} invalid{bad}/{total}")
        for pid in self.watch:
            v = views[pid]
            fault = f" fault={v['fault']}/{v['fault_direction']}" if v.get("fault") not in (None, "NORMAL") else ""
            parts.append(f"{pid}={'ok' if v['valid'] else 'NO ' + str(v['reason'])}{fault}")
        change = " | ".join(parts)
        values = " ".join(f"{pid}={views[pid]['value']:.4g}" for pid in self.watch if isinstance(views[pid]["value"], (int, float)))
        return change, values

    def watch_log(self):
        """Communication-log entries as they appear and as an aggregate grows or ends."""
        for entry in self.runtime.owner.commlog.entries.values():
            key = (entry.count, entry.state, entry.end_reason)
            if self.log_seen.get(entry.seq) != key:
                self.log_seen[entry.seq] = key
                d = entry.as_dict()
                self.say(
                    f"LOG #{d['seq']} {d['source_id']} {d['event']} reason={d['reason']} gen={d['conn_gen']} scope={d['scope']} count={d['count']} "
                    f"state={d['state']} end={d['end_reason']} silent={d['silent_seconds'] if d['silent_seconds'] is None else round(d['silent_seconds'], 2)}s detail={d['detail']}"
                )

    async def watching(self, until: float, every: float = 0.1, heartbeat: float = 10.0):
        last, beat = None, -1e9
        while self.now() < until:
            change, values = self.picture()
            if change != last or self.now() - beat >= heartbeat:
                self.say(f"{change}  [{values}]")
                if change != last:
                    self.transitions.append({"t": round(self.now(), 3), "state": change})
                last, beat = change, self.now()
            self.watch_log()
            await asyncio.sleep(every)

    # ------------------------------------------------------------------ the situations
    async def make(self):
        cfg, case = self.cfg, self.cfg["situation"]
        start, duration = float(cfg.get("start", 5)), float(cfg.get("duration", 5))
        await self.watching(start)
        if case in ("modbus_delay", "modbus_exception", "modbus_silent"):
            self.armed = case
            if case == "modbus_delay":
                self.mb.delay = float(cfg.get("delay", 1.5))
            elif case == "modbus_silent":
                self.mb.withhold = 10**9
            else:
                self.mb.exception = True
            self.mark(f"SET {case} {({k: v for k, v in cfg.items() if k in ('delay',)})}")
            await self.watching(start + duration)
            self.mb.delay, self.mb.exception, self.mb.withhold, self.armed = 0.0, False, 0, None
            self.mark(f"TAKEN AWAY {case}")
        elif case == "loop_stall":
            await asyncio.sleep(0)
            self.mark(f"SET loop stall: the process blocks for {duration} s")
            before = time.monotonic()
            time.sleep(duration)
            self.mark(f"STALL OVER after {time.monotonic() - before:.2f} s", effective=True)
        elif case == "modbus_slaves":
            modes = {int(k): v for k, v in cfg.get("modes", {"2": "SILENT"}).items()}
            old = dict(self.mb.mode)
            self.armed = "slave modes"
            self.mb.mode.update(modes)
            self.mark(f"SET slave modes {modes}")
            await self.watching(start + duration)
            self.mb.mode.update(old)
            self.armed = None
            self.mark(f"TAKEN AWAY slave modes (back to {old})")
        elif case == "ua_silence":
            self.proxy.pause()
            self.mark("SET silence: the proxy holds all traffic, the connections stay open")
            await self.watching(start + duration)
            self.proxy.resume()
            self.mark("TAKEN AWAY silence: the proxy forwards again")
        elif case == "ua_write":
            writes = sorted(cfg.get("writes", []), key=lambda w: w["at"])
            for w in writes:
                await self.watching(start + float(w["at"]), every=0.05)
                await self.ua.set(w["point"], w["value"], w.get("good", True))
                self.mark(f"WRITTEN {w['point']} = {w['value']} (good={w.get('good', True)}); the server holds it from now on", effective=True)
            await self.watching(start + duration)
            if cfg.get("restore") and writes:
                for point in dict.fromkeys(w["point"] for w in writes):
                    await self.ua.set(point, self.nominal[point])
                self.mark("RESTORED the written points to their old values", effective=True)
        elif case == "ua_gap":
            skip = int(cfg.get("skip", 1))
            for sub in self.ua.server.iserver.subscription_service.subscriptions.values():
                sub._notification_seq += skip
            self.mark(f"SKIPPED {skip} notification sequence number(s) on the server (a message that never was)", effective=True)
        elif case == "ua_overflow":
            point, burst = cfg.get("point", "temp_inner_01"), int(cfg.get("burst", 15))
            for i in range(burst):
                await self.ua.set(point, self.nominal[point] + 0.5 * (i + 1))
            self.mark(f"WRITTEN {burst} values to {point} at once; the queue holds {cfg.get('queue', 10)}", effective=True)
        elif case == "ua_server_params":
            self.mark("NOTHING TO MAKE: the parameters were revised at connect time (see the NEGOTIATED line)")
        await self.watching(start + duration + float(cfg.get("after", 15)))

    async def run(self):
        cfg = self.cfg
        deadline = time.monotonic() + 60
        while not self.all_valid():
            if time.monotonic() > deadline:
                raise SystemExit("not every input became valid within 60 s: nothing to make a situation on")
            await asyncio.sleep(0.1)
        self.t0 = time.monotonic()
        self.t0_wall = time.time()
        self.say(f"READY: all inputs valid.  situation={cfg['situation']}  watch={self.watch}")
        self.negotiated = self.runtime.diagnostics()
        for source_id, n in self.negotiated.items():
            granted = {k: v for k, v in n.items() if k != "items"}
            self.say(f"NEGOTIATED {source_id}: {json.dumps(granted, ensure_ascii=False, default=str)}")
        await self.make()

    async def archive_view(self) -> dict:
        """What the history API answers for the watched points over the whole run, as runs of equal rows:
        {point: [segments [{start, end, rows: [(first t, last t, count, state)]}]]} with t in seconds of the timeline."""
        length = self.now() + 10
        query = {"center": time.time() - length / 2 + 5, "seconds": length, "points": ",".join(self.watch)}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://htsg") as client:
            body = (await client.get("/api/replay", params=query, headers={"Authorization": "Bearer " + self.viewer})).json()
        rel = lambda wall: None if wall is None else round(wall - self.t0_wall, 2)  # noqa: E731
        deadline = time.monotonic() + 10
        while self.runtime.queue.depth and time.monotonic() < deadline:  # the groups still waiting for the writer
            await asyncio.sleep(0.1)
        view = {}
        for pid, channel in body.get("channels", {}).items():
            view[pid] = []
            for seg in channel.get("segments", []):
                runs = []
                for row in seg.get("rows", []):
                    state = "ok" if row["valid"] else str(row.get("quality") or row.get("reason") or row.get("status"))
                    if runs and runs[-1][3] == state:
                        runs[-1][1], runs[-1][2] = rel(row["time"]), runs[-1][2] + 1
                    else:
                        runs.append([rel(row["time"]), rel(row["time"]), 1, state])
                view[pid].append({"seg": seg["seg"], "start": rel(seg["start_wall"]), "end": rel(seg["end_wall"]), "end_reason": seg["end_reason"], "rows": runs})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://htsg") as client:
            listed = (await client.get("/api/faults", params={"start": self.t0_wall - 60, "end": time.time() + 5}, headers={"Authorization": "Bearer " + self.viewer})).json()
        faults = [
            {"point": f["point_id"], "direction": f["direction"], "basis_value": f["basis_value"], "basis_seen": rel(f["basis_wall"]),
             "confirmed": rel(f["confirmed_wall"]), "after_seconds": round(f["confirmed_wall"] - f["basis_wall"], 2)}
            for f in listed.get("items", [])
        ]
        ticks = [n for _, n in self.rounds]
        skipped = [n for n in range(ticks[0], ticks[-1] + 1) if n not in set(ticks)] if ticks else []
        return {"gaps": body.get("gaps"), "partial": body.get("partial"), "channels": view, "faults": faults,
                "rounds": {"count": len(ticks), "first": ticks[0] if ticks else None, "last": ticks[-1] if ticks else None, "skipped_planned": skipped,
                           "around_skips": [r for r in self.rounds if any(abs(r[1] - k) <= 1 for k in skipped)]}}


def listing() -> str:
    return "\n".join(f"{name:<18}{text}" for name, text in SITUATIONS.items())


def read_config(path: Path) -> dict:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise SystemExit(f"No configuration at {path}.  Create it, e.g. " + '{"situation": "modbus_delay", "start": 10, "duration": 4, "after": 20}') from None
    except json.JSONDecodeError as error:
        raise SystemExit(f"{path} is not valid JSON: {error}") from None
    if not isinstance(cfg, dict) or cfg.get("situation") not in SITUATIONS:
        raise SystemExit(f'{path}: "situation" must be one of: {", ".join(SITUATIONS)}')
    return cfg


async def main_async(cfg: dict, out: Path | None, argv: list[str]):
    obs = Observation(cfg, out)
    await obs.start()
    failed = None
    try:
        await obs.run()
        archive = await obs.archive_view()
        obs.say("ARCHIVE VIEW (rows as [first t, last t, count, state]; a new row is stored when the value or its state changes):")
        rounds = (archive or {}).get("rounds")
        if rounds and rounds["count"]:
            obs.say(f"  MODBUS ROUNDS: {rounds['count']} committed, planned ticks {rounds['first']}..{rounds['last']}, skipped planned ticks {rounds['skipped_planned']}, around the skips (t, tick) {rounds['around_skips']}")
        for fault in (archive or {}).get("faults", []):
            obs.say(f"  FAULT {fault['point']} {fault['direction']}: value {fault['basis_value']} first seen +{fault['basis_seen']}, confirmed +{fault['confirmed']} ({fault['after_seconds']} s later)")
        for pid, segments in (archive or {}).get("channels", {}).items():
            for seg in segments:
                obs.say(f"  {pid} seg{seg['seg']} {seg['start']}..{seg['end']} {seg['end_reason']}: " + " ".join(str(r) for r in seg["rows"]))
    except BaseException as error:  # noqa: BLE001 - reported, then the servers and databases are still taken down
        failed, archive = repr(error), None
        raise
    finally:
        commit_info = {
            "commit": git("rev-parse", "HEAD"),
            "dirty_candidate_files": git("status", "--porcelain", "--", "htsg", "config"),
        }
        evidence = {
            "command": " ".join([Path(sys.argv[0]).name, *argv]), **commit_info, "config": cfg,
            "started_wall": getattr(obs, "t0_wall", None), "markers": obs.markers, "transitions": obs.transitions,
            "negotiated": getattr(obs, "negotiated", None),
            "commlog": [e.as_dict() for e in obs.runtime.owner.commlog.entries.values()],
            "sources": obs.runtime.owner.status()["sources"], "connect_failures": obs.runtime.connect_failures(),
            "archive_view": archive, "failed": failed,
        }  # fmt: skip
        await obs.stop()
        evidence["runtime_health"] = obs.runtime.health
        if out:
            out.mkdir(parents=True, exist_ok=True)
            (out / "timeline.txt").write_text("\n".join(obs.lines) + "\n", encoding="utf-8")
            (out / "evidence.json").write_text(json.dumps(evidence, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
            print(f"timeline.txt and evidence.json written to {out}")
    print(f"STOPPED: {obs.runtime.health}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="the situation file (JSON); default .local/observe/observe.json")
    parser.add_argument("--out", type=Path, help="directory that receives timeline.txt and evidence.json")
    parser.add_argument("--list", action="store_true", help="list the situations and exit")
    args = parser.parse_args(argv)
    if args.list:
        print(listing())
        return
    asyncio.run(main_async(read_config(Path(args.config)), args.out, sys.argv[1:] if argv is None else list(argv)))


if __name__ == "__main__":
    main()
