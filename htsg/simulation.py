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


def signal_value(signal, seed, step, interval):
    noise = random.Random(f"{seed}:{signal.point}:{step}").uniform(-signal.noise, signal.noise)
    return (
        signal.base
        + signal.amplitude * math.sin(2 * math.pi * step * interval / signal.period + signal.phase)
        + noise
    )


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
    def __init__(self, config):
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
        self.scenario = self.scenarios[name]
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

        server = Server()
        await server.init()
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

    def generate(self, now):
        scenario = self.scenario
        elapsed = time.monotonic() - self.scenario_started
        if scenario.kind != "baseline" and elapsed >= scenario.duration:
            self.set_scenario(self.baseline.name)
            scenario = self.scenario
            elapsed = 0
        for signal in self.sim.signals:
            if scenario.kind == "freeze" and signal.point in scenario.points and signal.point in self.values:
                continue
            value = signal_value(signal, self.sim.seed, self.step, self.sim.interval)
            if scenario.kind == "thermal_step":
                value += scenario.offsets.get(signal.point, 0)
            elif scenario.kind == "load_cycle":
                value += scenario.offsets.get(signal.point, 0) * math.sin(
                    math.pi * min(elapsed / scenario.duration, 1)
                )
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
            while True:
                self.generate(time.time())
                await self.reconcile_servers()
                if self.opcua:
                    await self.write_opcua()
                await asyncio.sleep(self.sim.interval)
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
            "freeze_note": "Freeze retains value and OPC UA source timestamp; received-time mode cannot diagnose frozen device values solely from successful polls.",
        }


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str


def create_simulation_app(config):
    token = os.environ.get(config.simulation.token_env, "")
    if len(token) < 16:
        raise ValueError("simulation control token must contain at least 16 characters")
    field = FieldSimulator(config)

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
    args = parser.parse_args()
    config = load_settings(args.config)
    uvicorn.run(
        create_simulation_app(config),
        host=config.simulation.control_host,
        port=config.simulation.control_port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
