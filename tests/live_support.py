"""Full-scale live rig: real Modbus + OPC UA servers, both acquirers, ordered ingress, one owner."""

import asyncio
import time
from pathlib import Path

from protocol_support import FieldModbus, FieldUa, PausableProxy

from htsg.config import Settings, Timing, load_settings
from htsg.ingress import Ingress
from htsg.modbus import ModbusAcquirer
from htsg.owner import Owner
from htsg.ua import UaAcquirer

FULL = load_settings(Path(__file__).resolve().parents[1] / "config" / "research-sim.json")
UA_NAMES = [p.id for p in FULL.points if p.source == "ua_field"]
BRIDGES = [p.id for p in FULL.points if p.source == "mb_field"]
PROCESS = {
    "load_mw": 600.0, "waterwall_flow": 1850.0, "main_steam_flow": 2117.0, "main_steam_pressure": 28.7,
    "feedwater_temperature": 309.0, "reheat_temperature": 623.0, "working_fluid_temperature": 435.0,
    "furnace_pressure": -0.05,
}  # fmt: skip


def full_config(mb_port, ua_endpoint, timing=None):
    payload = FULL.model_dump(mode="json")
    payload["simulation"]["enabled"] = False
    payload["sources"][0]["port"] = mb_port
    payload["sources"][1]["endpoint"] = ua_endpoint
    if timing is not None:
        payload["timing"] = timing.model_dump()
    return Settings.model_validate(payload)


class Rig:
    """Everything wired as the runtime will wire it, with in-memory commit/publish sinks."""

    def __init__(self, timing=None, use_proxy=False):
        # FieldUa installs the project-local server adapter, so the normal contract default can be tested directly.
        self.timing = timing or Timing()
        self.use_proxy = use_proxy
        self.mb, self.ua = FieldModbus(), FieldUa(UA_NAMES)
        self.proxy = None
        self.commits, self.pubs = [], []

    async def seed(self):
        """Distinct nominal values, written BEFORE any subscription exists so the first delivery is final."""
        for i, name in enumerate(UA_NAMES):
            if name.startswith("temp_inner"):
                await self.ua.set(name, 480.0 + i * 0.01)
            elif name.startswith("temp_outer"):
                await self.ua.set(name, 440.0 + i * 0.01)
            else:
                await self.ua.set(name, PROCESS[name])

    async def __aenter__(self):
        await self.mb.start()
        await self.ua.start()
        await self.seed()
        endpoint = self.ua.endpoint
        if self.use_proxy:
            self.proxy = PausableProxy(self.ua.port)
            await self.proxy.start()
            endpoint = f"opc.tcp://127.0.0.1:{self.proxy.port}/t/"
        self.config = full_config(self.mb.port, endpoint, self.timing)
        self.owner = Owner(
            self.config, time.time(), time.monotonic(), commit=self.commits.append, publish=self.pubs.append,
            run_id="live",
        )  # fmt: skip
        self.ingress = Ingress(self.owner, 4096)
        by_source = {s.id: [p for p in self.config.points if p.source == s.id] for s in self.config.sources}
        self.mba = ModbusAcquirer(self.config.sources[0], by_source["mb_field"], self.timing, self.ingress)
        self.uaa = UaAcquirer(self.config.sources[1], by_source["ua_field"], self.timing, self.ingress)
        self.owner.on_rebuild = self.uaa.request_rebuild
        self.tasks = [
            asyncio.create_task(self.mba.run()),
            asyncio.create_task(self.uaa.run()),
            asyncio.create_task(self._loop()),
        ]
        return self

    async def _loop(self):
        last = time.monotonic()
        while True:
            self.ingress.drain()
            now = time.monotonic()
            if now - last >= 1.0:
                last = now
                self.owner.tick(time.time(), now)
            await asyncio.sleep(0.05)

    async def __aexit__(self, *exc):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.proxy:
            await self.proxy.stop()
        await self.ua.stop()
        await self.mb.stop()

    async def until(self, condition, timeout=20.0, why=""):
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline, f"timed out: {why}"
            await asyncio.sleep(0.05)

    # -- convenience views -------------------------------------------------------------------------

    def observations(self, source=None):
        return [o for g in self.commits for o in g.observations if source in (None, o.source_id)]

    def groups(self, kind):
        return [g for g in self.commits if g.kind == kind]
