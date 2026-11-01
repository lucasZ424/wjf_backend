"""Independent read-only protocol field simulator; never calls the backend engine."""

from __future__ import annotations

import argparse
import asyncio
import hmac
import math
import os
import random
import struct
import time
from collections import deque
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from functools import partial

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict

from .config import load_settings
from .research_config import estimate_budget, validate_research
from .timing import every_second


def signal_value(signal, seed, step, interval):
    noise = random.Random(f"{seed}:{signal.point}:{step}").uniform(-signal.noise, signal.noise)
    return (
        signal.base
        + signal.amplitude * math.sin(2 * math.pi * step * interval / signal.period + signal.phase)
        + noise
    )


# The low-order unit model (修复轮15).  A 660 MW once-through unit around its 600 MW reference point: every value below is
# written for that point, and the relations hold for mid and high loads only, not for start-up or deep low load.  The wall
# stations are taken as heated waterwall tube sections: the flue gas heats the outer wall and the tube passes the heat to
# the fluid inside, so the outer wall is the hotter one.  Demonstration figures, not the guaranteed values of a real unit.
REFERENCE_MW = 600.0
REFERENCE = {
    "waterwall_flow": 1850.0,  # t/h, proportional to the load
    "main_steam_flow": 2117.0,  # t/h, proportional to the load
    "main_steam_pressure": 28.7,  # MPa, sliding 0.02 MPa per MW
    "feedwater_temperature": 309.0,  # degC, 0.1 K per MW
    "working_fluid_temperature": 435.0,  # degC, a few degrees away while the load moves
    "reheat_temperature": 623.0,  # degC, likewise
    "furnace_pressure": -0.05,  # kPa, turbulence only
}
MINIMUM_WATERWALL_FLOW = 0.55  # of the reference flow: a once-through boiler keeps a minimum waterwall flow
SEGMENT_HEAT = (0.55, 0.8, 1.0, 1.0, 0.9, 0.75, 0.6, 0.45)  # relative heat of H1 (bottom) … H8 (top); burners at H3-H4
INLET_RISE = 51.0  # K: the fluid enters the waterwall this much above the feedwater temperature (360 degC at reference)
FILM_K = 40.0  # K: inner wall above the fluid at relative heat 1 and reference flow
WALL_K = 30.0  # K: outer wall above the inner wall at relative heat 1
STRAIN_PER_K = 20.0  # με per K that a station's wall difference moves from its reference (demonstration coupling)
STRAIN_PER_MPA = 24.0  # με per MPa that the main steam pressure moves from its reference (demonstration coupling)
TAU = {"load": 10, "flow": 20, "pressure": 60, "feedwater": 300, "fluid": 120, "wall": 90}  # first-order time constants, s


PROCESS_KINDS = ("load_ramp", "hotspot")  # the scenarios that act through the plant model
# How each quantity is sent: (demonstration: noise spread, noise memory, resolution) and (steady: noise spread, noise memory,
# dead band).  A resolution of 0 sends the value as it is (a bridge in mV/V); a dead band of 0.0006 mV/V is 0.1 με.
NOISE = {
    "load_mw": ((0.3, 0.95, 0.1), (0.3, 0.95, 0.5)),
    "waterwall_flow": ((2.0, 0.95, 0.1), (1.5, 0.95, 2.0)),
    "main_steam_flow": ((2.0, 0.95, 0.1), (1.5, 0.95, 2.0)),
    "main_steam_pressure": ((0.01, 0.95, 0.001), (0.004, 0.95, 0.01)),
    "feedwater_temperature": ((0.1, 0.95, 0.1), (0.04, 0.99, 0.1)),
    "working_fluid_temperature": ((0.2, 0.95, 0.1), (0.04, 0.99, 0.1)),
    "reheat_temperature": ((0.2, 0.95, 0.1), (0.04, 0.99, 0.1)),
    "furnace_pressure": ((0.02, 0.7, 0.001), (0.01, 0.9, 0.005)),
    "temp_inner": ((0.15, 0.95, 0.1), (0.04, 0.99, 0.1)),
    "temp_outer": ((0.15, 0.95, 0.1), (0.04, 0.99, 0.1)),
    "bridge_mvv": ((0.0006, 0.95, 0), (0.0002, 0.99, 0.0006)),
}


def lag(state, target, tau):
    """One second of a first-order lag: a step reaches 63 % after `tau` seconds, never a delay and then a jump."""
    return state + (target - state) * (1 - math.exp(-1 / tau))


def pair_heat(index):
    """The relative heat at temperature pair `index` (1-96) at the reference load: its segment's share and a fixed
    unevenness of up to 3 % around the furnace (segment (i-1)%8+1, group (i-1)//8+1, as in 模拟通道分配)."""
    segment, group = (index - 1) % 8, (index - 1) // 8
    return SEGMENT_HEAT[segment] * (1 + 0.03 * math.sin(2.4 * group + 0.7 * segment))


class Plant:
    """Every modelled value follows the load or the local heat through a first-order lag, plus small correlated noise."""

    def __init__(self, seed, steady=False):
        self.random = random.Random(f"{seed}:plant")
        self.steady = steady
        self.noise = {}
        self.sent = {}
        self.state = None
        self.command = None

    def wobble(self, key, sigma, rho=0.95):
        """Correlated noise: each second keeps `rho` of the last one, with a steady spread of `sigma`."""
        value = rho * self.noise.get(key, 0.0) + sigma * math.sqrt(1 - rho * rho) * self.random.gauss(0, 1)
        self.noise[key] = value
        return value

    def base_load(self, step):
        """Slow swings of tens of minutes and a little drift around the reference: about 588-612 MW."""
        return (
            REFERENCE_MW
            + 8 * math.sin(2 * math.pi * step / 2400)
            + 3 * math.sin(2 * math.pi * step / 900 + 1)
            + self.wobble("load-drift", 1.0, 0.995)
        )

    def targets(self, load, flow, feedwater, fluid, heat):
        """Where every quantity heads at this load: the process values, then the 96 wall pairs."""
        ratio = load / REFERENCE_MW
        out = {
            "waterwall_flow": REFERENCE["waterwall_flow"] * max(ratio, MINIMUM_WATERWALL_FLOW),
            "main_steam_flow": REFERENCE["main_steam_flow"] * ratio,
            "main_steam_pressure": REFERENCE["main_steam_pressure"] + 0.02 * (load - REFERENCE_MW),
            "feedwater_temperature": REFERENCE["feedwater_temperature"] + 0.1 * (load - REFERENCE_MW),
        }
        cooling = (flow / REFERENCE["waterwall_flow"]) ** 0.8  # more fluid takes more heat away from the inner wall
        inlet = feedwater + INLET_RISE
        for i in range(1, 97):
            q = pair_heat(i) * ratio * (1 + heat.get(i, 0.0))
            inner = inlet + (fluid - inlet) * ((i - 1) % 8) / 7 + FILM_K * q / cooling
            out[f"temp_inner_{i:02d}"] = inner
            out[f"temp_outer_{i:02d}"] = inner + WALL_K * q
        return out

    def advance(self, step, command, heat):
        """One second toward the load `command` (MW), with `heat` mapping a pair index to its added local heat."""
        rate = 0.0 if self.command is None else (command - self.command) * 60  # MW per minute
        self.command = command
        if self.state is None:  # start settled at the first load
            s = {"load": command}
            s["working_fluid_temperature"] = REFERENCE["working_fluid_temperature"]
            s["reheat_temperature"] = REFERENCE["reheat_temperature"]
            flow = REFERENCE["waterwall_flow"] * max(command / REFERENCE_MW, MINIMUM_WATERWALL_FLOW)
            feedwater = REFERENCE["feedwater_temperature"] + 0.1 * (command - REFERENCE_MW)
            s.update(self.targets(command, flow, feedwater, s["working_fluid_temperature"], heat))
            self.state = s
        s = self.state
        s["load"] = lag(s["load"], command, TAU["load"])
        fluid = REFERENCE["working_fluid_temperature"] + 0.3 * rate
        s["working_fluid_temperature"] = lag(s["working_fluid_temperature"], fluid, TAU["fluid"])
        reheat = REFERENCE["reheat_temperature"] + 0.2 * rate
        s["reheat_temperature"] = lag(s["reheat_temperature"], reheat, TAU["fluid"])
        target = self.targets(s["load"], s["waterwall_flow"], s["feedwater_temperature"], s["working_fluid_temperature"], heat)
        for name in ("waterwall_flow", "main_steam_flow"):
            s[name] = lag(s[name], target[name], TAU["flow"])
        s["main_steam_pressure"] = lag(s["main_steam_pressure"], target["main_steam_pressure"], TAU["pressure"])
        s["feedwater_temperature"] = lag(s["feedwater_temperature"], target["feedwater_temperature"], TAU["feedwater"])
        for i in range(1, 97):
            for side in ("inner", "outer"):
                name = f"temp_{side}_{i:02d}"
                s[name] = lag(s[name], target[name], TAU["wall"])
        return self.readings()

    def send(self, name, value, band):
        """A dead band, as a DCS sends on: the reading moves only once the quantity is a whole step away from the last one
        sent, so a slow quantity holds its value for tens of seconds instead of flickering in its last digit."""
        last = self.sent.get(name)
        if last is None or abs(value - last) >= band:
            last = self.sent[name] = round(round(value / band) * band, 6)
        return last

    def readings(self):
        """What is sent.  Demonstration: the state with visible noise at the resolution a DCS shows, so the curves move.
        Steady: the state with a little slow noise through a dead band, so a wall holds its reading for tens of seconds, as a
        real plant at steady load does (用户 11-01: the steady field for the tests, the demonstration one for showing)."""
        s, out = self.state, {}
        values = {"load_mw": s["load"], "furnace_pressure": REFERENCE["furnace_pressure"]}
        for n in range(1, 9):  # station n is pair n: a reference strain, its wall difference and the pressure move it
            difference = s[f"temp_inner_{n:02d}"] - s[f"temp_outer_{n:02d}"]
            strain = (
                110
                + 8 * n
                + STRAIN_PER_K * (difference + WALL_K * pair_heat(n))
                + STRAIN_PER_MPA * (s["main_steam_pressure"] - REFERENCE["main_steam_pressure"])
            )
            values[f"bridge_mvv_{n:02d}"] = strain * 6 / 1000
        for name in NOISE:
            if name not in values and name in s:
                values[name] = s[name]
        for i in range(1, 97):
            for side in ("inner", "outer"):
                values[f"temp_{side}_{i:02d}"] = s[f"temp_{side}_{i:02d}"]
        for name, value in values.items():
            kind = name if name in NOISE else name.rsplit("_", 1)[0]
            demo, steady = NOISE[kind]
            if self.steady:
                sigma, rho, band = steady
                out[name] = self.send(name, value + self.wobble(name, sigma, rho), band)
            else:
                sigma, rho, step = demo
                noisy = value + self.wobble(name, sigma, rho)
                out[name] = round(round(noisy / step) * step, 6) if step else noisy
        return out


def encode_registers(value, point):
    """Convert engineering value back to the configured wire representation."""
    raw = (value - point.offset) / point.scale
    fmt = {"uint16": "H", "int16": "h", "uint32": "I", "int32": "i", "float32": "f", "float64": "d"}[
        point.data_type
    ]
    if point.data_type not in ("float32", "float64"):
        raw = round(raw)
    wire = struct.pack(">" + fmt, raw)
    words = [int.from_bytes(wire[i : i + 2], point.byte_order) for i in range(0, len(wire), 2)]
    return words[::-1] if point.word_order == "little" else words


class FieldSimulator:
    def __init__(self, config, write_ceiling=False, steady=False):
        validate_research(config)
        if not config.simulation.enabled:
            raise ValueError("simulation is disabled")
        self.config = config
        self.sim = config.simulation
        self.scenarios = {s.name: s for s in self.sim.scenarios}
        self.baseline = next(s for s in self.sim.scenarios if s.kind == "baseline")
        self.scenario = self.baseline
        self.scenario_started = time.monotonic()
        self.started_at = time.time()
        self.step = 0
        self.values = {}
        # write_ceiling (development load tests only, never a field mode): every input changes every second through its
        # independent signal - not what a plant does, but the most a gateway sending raw noisy values could make the
        # backend write (修复轮15, 用户 11-01).  Otherwise the plant model, and only then the process scenarios.
        self.write_ceiling = write_ceiling
        self.plant = Plant(self.sim.seed, steady)  # steady: the plant at the pace of a real one, for the tests
        self.ramp_from = None
        self.ledger = deque(maxlen=self.sim.ledger_limit)
        self.transitions = deque(maxlen=self.sim.ledger_limit)
        self.modbus = None
        self.opcua = None
        self.nodes = {}
        self.task = None
        self.error = None
        self.sources = {s.kind: s for s in config.sources}
        self.point_map = {p.id: p for p in config.points}
        self.source_kinds = {s.id: s.kind for s in config.sources}

    def set_scenario(self, name):
        if name not in self.scenarios:
            raise ValueError("unknown scenario")
        if self.write_ceiling and self.scenarios[name].kind in PROCESS_KINDS:
            raise ValueError("process scenarios need the plant model; this field runs the write-ceiling load")
        self.scenario = self.scenarios[name]
        self.ramp_from = None
        self.scenario_started = time.monotonic()
        self.transitions.append({"time": time.time(), "scenario": name, "step": self.step})

    async def modbus_action(
        self, device_id, function_code, start_address, address, count, registers, set_values
    ):
        from pymodbus.constants import ExcCodes

        if set_values is not None or function_code not in (3, 4):
            return ExcCodes.ILLEGAL_FUNCTION
        kind = "holding" if function_code == 3 else "input"
        for point in self.config.points:
            if (
                self.source_kinds[point.source] != "modbus"
                or point.device_id != device_id
                or point.register_kind != kind
            ):
                continue
            data = self.values.get(point.id)
            if data is None:
                return ExcCodes.DEVICE_BUSY
            if data["quality"] != "GOOD":
                if point.data_type in ("float32", "float64"):
                    value = math.nan
                elif point.invalid_values:
                    value = point.invalid_values[0] * point.scale + point.offset
                else:
                    if point.address < address + count and point.address + 2 > address:
                        return ExcCodes.DEVICE_FAILURE
                    continue
            else:
                value = data["value"]
            words = encode_registers(value, point)
            index = point.address - start_address
            if 0 <= index and index + len(words) <= len(registers):
                registers[index : index + len(words)] = words
        return None

    async def start_modbus(self):
        from pymodbus.server import ModbusTcpServer
        from pymodbus.simulator import DataType, SimData, SimDevice

        devices = []
        points = [p for p in self.config.points if self.source_kinds[p.source] == "modbus"]
        for identity in sorted({p.device_id for p in points}):
            blocks = []
            for kind in ("holding", "input"):
                blocks.append(
                    [
                        SimData(
                            p.address,
                            values=[0]
                            * {"uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2, "float64": 4}[
                                p.data_type
                            ],
                            datatype=DataType.REGISTERS,
                            readonly=True,
                        )
                        for p in points
                        if p.device_id == identity and p.register_kind == kind
                    ]
                    or [SimData(0, values=0, datatype=DataType.INVALID)]
                )
            devices.append(
                SimDevice(
                    identity,
                    simdata=(
                        [SimData(0, values=False, datatype=DataType.BITS)],
                        [SimData(0, values=False, datatype=DataType.BITS)],
                        *blocks,
                    ),
                    action=partial(self.modbus_action, identity),
                )
            )
        source = self.sources["modbus"]
        server = ModbusTcpServer(devices, address=(source.host, source.port))
        await server.serve_forever(background=True)
        self.modbus = server

    async def start_opcua(self):
        from asyncua import Server, ua

        from .sim_ua import install_keepalive_adapter

        server = Server()
        await server.init()
        install_keepalive_adapter(server)
        server.set_endpoint(self.sources["opcua"].endpoint)
        server.set_server_name("HTSG synthetic research field")
        server.set_security_policy([ua.SecurityPolicyType.NoSecurity])
        index = await server.register_namespace(self.sim.namespace_uri)
        if index != 2:
            raise RuntimeError("unexpected simulator namespace")
        container = await server.nodes.objects.add_object(index, "Research")
        self.nodes = {}
        for p in self.config.points:
            if self.source_kinds[p.source] == "opcua":
                self.nodes[p.id] = await container.add_variable(p.node_id, p.id, 0.0)
        self.opcua = server
        await self.write_opcua()
        try:
            await server.start()
        except BaseException:
            await server.stop()
            self.opcua = None
            raise

    async def write_opcua(self):
        from asyncua import ua

        for name, node in self.nodes.items():
            data = self.values.get(name)
            if data is None:
                continue
            p = self.point_map[name]
            raw = (data["value"] - p.offset) / p.scale
            dv = ua.DataValue(
                ua.Variant(raw, ua.VariantType.Double),
                ua.StatusCode(
                    ua.StatusCodes.Good if data["quality"] == "GOOD" else ua.StatusCodes.BadSensorFailure
                ),
                SourceTimestamp=datetime.fromtimestamp(data["source_time"], UTC),
            )
            await node.write_value(dv)

    def load_command(self, scenario, elapsed):
        """The base swing, or a load ramp: from where the load was, at `ramp_rate`, to the target, held, and back."""
        base = self.plant.base_load(self.step)
        if scenario.kind != "load_ramp":
            return base
        if self.ramp_from is None:
            self.ramp_from = base
        ramp = max(abs(scenario.load_target - self.ramp_from) / scenario.ramp_rate * 60, 1)
        share = max(0.0, min(1.0, elapsed / ramp, (scenario.duration - elapsed) / ramp))
        return base + share * (scenario.load_target - base)

    def local_heat(self, scenario):
        """Pair index -> added local heat while a hotspot lasts; the walls follow through their own lag."""
        if scenario.kind != "hotspot":
            return {}
        return {int(point.rsplit("_", 1)[1]): scenario.heat for point in scenario.points}

    def generate(self, now):
        scenario = self.scenario
        elapsed = time.monotonic() - self.scenario_started
        if scenario.kind != "baseline" and elapsed >= scenario.duration:
            self.set_scenario(self.baseline.name)
            scenario = self.scenario
            elapsed = 0
        modelled = {} if self.write_ceiling else self.plant.advance(
            self.step, self.load_command(scenario, elapsed), self.local_heat(scenario)
        )
        for signal in self.sim.signals:
            if scenario.kind == "freeze" and signal.point in scenario.points and signal.point in self.values:
                continue
            if signal.point in modelled:
                value = modelled[signal.point]
            else:
                value = signal_value(signal, self.sim.seed, self.step, self.sim.interval)
            if scenario.kind == "thermal_step":
                value += scenario.offsets.get(signal.point, 0)
            self.values[signal.point] = {
                "value": value,
                "quality": "BAD"
                if scenario.kind == "bad_quality" and signal.point in scenario.points
                else "GOOD",
                "source_time": now,
            }
        self.ledger.append(
            {
                "step": self.step,
                "time": now,
                "scenario": scenario.name,
                "config_id": self.config.config_id,
                "values": {name: dict(value) for name, value in self.values.items()},
            }
        )
        self.step += 1

    async def reconcile_servers(self):
        for kind in ("modbus", "opcua"):
            server = getattr(self, kind)
            disconnected = self.scenario.kind == f"disconnect_{kind}"
            if disconnected and server:
                await (server.shutdown() if kind == "modbus" else server.stop())
                setattr(self, kind, None)
                if kind == "opcua":
                    self.nodes = {}
            elif not disconnected and server is None:
                await (self.start_modbus() if kind == "modbus" else self.start_opcua())

    async def run(self):
        try:
            async for _ in every_second(immediate=False):
                self.generate(time.time())
                await self.reconcile_servers()
                if self.opcua:
                    await self.write_opcua()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.error = type(error).__name__
            await self.close_servers()
            raise

    async def start(self):
        self.generate(time.time())
        try:
            await self.reconcile_servers()
        except BaseException:
            await self.close_servers()
            raise
        self.task = asyncio.create_task(self.run(), name="research-field-generator")

    async def close_servers(self):
        for kind in ("modbus", "opcua"):
            server = getattr(self, kind)
            if server:
                await (server.shutdown() if kind == "modbus" else server.stop())
                setattr(self, kind, None)
        self.nodes = {}

    async def stop(self):
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        await self.close_servers()

    def status(self):
        return {
            "config_id": self.config.config_id,
            "mode": "write_ceiling" if self.write_ceiling else "steady" if self.plant.steady else "demonstration",
            "scenario": self.scenario.name,
            "step": self.step,
            "started_at": self.started_at,
            "error": self.error,
            "servers": {"modbus": self.modbus is not None, "opcua": self.opcua is not None},
            "values": self.values,
            "transitions": list(self.transitions),
            "ledger_retained": len(self.ledger),
            "ledger_limit": self.sim.ledger_limit,
            "budget": estimate_budget(self.config),
            "freeze_note": "Freeze retains value and OPC UA source timestamp; received-time mode cannot diagnose frozen device values while the subscription remains healthy.",
        }


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str


def create_simulation_app(config, steady=False):
    token = os.environ.get(config.simulation.token_env, "")
    if len(token) < 16:
        raise ValueError("simulation control token must contain at least 16 characters")
    field = FieldSimulator(config, steady=steady)

    @asynccontextmanager
    async def lifespan(app):
        await field.start()
        try:
            yield
        finally:
            await field.stop()

    app = FastAPI(title="HTSG independent simulated field", lifespan=lifespan)
    app.state.field = field

    def authorize(authorization):
        if not hmac.compare_digest(authorization or "", "Bearer " + token):
            raise HTTPException(401, "invalid simulator control credential")

    @app.get("/status")
    async def status(authorization: str | None = Header(None)):
        authorize(authorization)
        return field.status()

    @app.get("/ledger")
    async def ledger(authorization: str | None = Header(None)):
        authorize(authorization)
        return {"config_id": config.config_id, "items": list(field.ledger), "bounded": True}

    @app.post("/scenario")
    async def scenario(request: ScenarioRequest, authorization: str | None = Header(None)):
        authorize(authorization)
        try:
            field.set_scenario(request.scenario)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return field.status()

    return app


def main():
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/research-sim.json")
    parser.add_argument("--steady", action="store_true", help="the plant at the pace of a real one (tests); default: demonstration")
    args = parser.parse_args()
    config = load_settings(args.config)
    uvicorn.run(
        create_simulation_app(config, args.steady),
        host=config.simulation.control_host,
        port=config.simulation.control_port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
