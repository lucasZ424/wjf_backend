"""Research-only simulator and presentation configuration; no plant credentials."""

from __future__ import annotations

import math
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field


class ResearchModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Project(ResearchModel):
    title: str = "HTSG 科研监测"
    rated_mw: float = Field(660, gt=0)
    notice: str = "科研模拟与估算，不代表现场实测"
    basis: str = ""


class Signal(ResearchModel):
    point: str
    base: float
    amplitude: float = Field(0, ge=0)
    period: float = Field(120, gt=0)
    noise: float = Field(0, ge=0)
    phase: float = 0


class Scenario(ResearchModel):
    name: str
    label: str
    kind: Literal[
        "baseline",
        "thermal_step",
        "load_cycle",
        "bad_quality",
        "freeze",
        "disconnect_modbus",
        "disconnect_opcua",
    ]
    duration: float = Field(30, gt=0, le=3600)
    points: tuple[str, ...] = ()
    offsets: dict[str, float] = Field(default_factory=dict)


class Simulation(ResearchModel):
    enabled: bool = False
    seed: int = 6602026
    interval: Literal[1] = 1
    control_host: Literal["127.0.0.1"] = "127.0.0.1"
    control_port: int = Field(14841, ge=1024, le=65535)
    token_env: str = "HTSG_SIM_TOKEN"
    namespace_uri: str = "urn:htsg:research"
    signals: tuple[Signal, ...] = ()
    scenarios: tuple[Scenario, ...] = ()
    ledger_limit: int = Field(120, ge=1, le=1000)
    validation_duration_seconds: float = Field(2100, ge=1, le=86400)


class Station(ResearchModel):
    id: str
    label: str
    x: float = Field(ge=0, le=100)
    y: float = Field(ge=0, le=100)
    strain: str
    strain_change: str | None = None
    temperature: str
    inner_temperature: str
    stress: str
    delta_temperature: str


class UI(ResearchModel):
    point_labels: dict[str, str] = Field(default_factory=dict)
    stations: tuple[Station, ...] = ()
    process_points: tuple[str, ...] = ()
    history_windows: tuple[int, ...] = (60, 300, 900)
    precision: int = Field(2, ge=0, le=6)


def estimate_budget(config):
    """Startup estimate including observed row/index overhead; runtime guards remain authoritative."""
    rates = {s.id: 1 / s.interval for s in config.sources if s.enabled}
    source_rate = sum(rates.get(p.source, 0) for p in config.points)
    seconds = config.simulation.validation_duration_seconds
    frame_bytes = seconds * (len(config.points) + len(config.derived)) * 1024
    raw_bytes = seconds * source_rate * 1024
    return {
        "source_samples_per_second": source_rate,
        "validation_bytes_estimate": math.ceil(frame_bytes + raw_bytes + 8 * 1024**2),
        "development_budget_bytes": config.storage.development_budget_bytes,
        "estimate_note": "1024 B/stored frame value including indexes, 1024 B/continuous raw sample, 8 MiB overhead; estimate only; excludes existing data and WAL",
    }


def validate_research(config):
    points = {p.id: p for p in config.points}
    known = set(points) | {p.id for p in config.derived}
    if not set(config.ui.point_labels) <= known:
        raise ValueError("UI labels reference unknown points")
    for station in config.ui.stations:
        refs = (
            station.strain,
            station.temperature,
            station.inner_temperature,
            station.stress,
            station.delta_temperature,
        )
        if station.strain_change is not None:
            refs = (*refs, station.strain_change)
        if not set(refs) <= known:
            raise ValueError("UI station references unknown points")
    if not set(config.ui.process_points) <= known or any(x <= 0 for x in config.ui.history_windows):
        raise ValueError("invalid UI process points/history windows")
    if len({s.id for s in config.ui.stations}) != len(config.ui.stations):
        raise ValueError("duplicate UI station identifiers")
    sim = config.simulation
    if not sim.enabled:
        return config
    if config.mode != "development":
        raise ValueError("research simulation requires development mode")
    signals = {s.point for s in sim.signals}
    if signals != set(points) or len(signals) != len(sim.signals):
        raise ValueError("simulation requires exactly one signal per source point")
    scenario_names = {s.name for s in sim.scenarios}
    if len(scenario_names) != len(sim.scenarios) or not any(s.kind == "baseline" for s in sim.scenarios):
        raise ValueError("simulation requires unique scenarios and a baseline")
    for scenario in sim.scenarios:
        if not (set(scenario.points) | set(scenario.offsets)) <= signals:
            raise ValueError("scenario references unknown point")
    ports = [sim.control_port]
    kinds = []
    occupied = set()
    for source in config.sources:
        if not source.enabled or source.kind not in ("modbus", "opcua"):
            raise ValueError("simulation sources must be enabled Modbus/OPC UA")
        kinds.append(source.kind)
        if source.kind == "modbus":
            if source.host != "127.0.0.1":
                raise ValueError("simulation Modbus must bind loopback")
            ports.append(source.port)
        else:
            endpoint = urlparse(source.endpoint)
            if endpoint.scheme != "opc.tcp" or endpoint.hostname != "127.0.0.1" or not endpoint.port:
                raise ValueError("simulation OPC UA must bind loopback")
            ports.append(endpoint.port)
    if sorted(kinds) != ["modbus", "opcua"] or len(set(ports)) != len(ports):
        raise ValueError("simulation needs one server per protocol and distinct ports")
    sources = {s.id: s for s in config.sources}
    ua_nodes = [p.node_id for p in config.points if sources[p.source].kind == "opcua"]
    if len(set(ua_nodes)) != len(ua_nodes):
        raise ValueError("duplicate simulation OPC UA NodeIds")
    if any(not any(p.source == s.id for p in config.points) for s in config.sources):
        raise ValueError("each simulation protocol requires at least one point")
    for p in config.points:
        if p.scale == 0:
            raise ValueError("simulation point scale cannot be zero")
        if sources[p.source].kind == "modbus":
            width = {"uint16": 1, "int16": 1, "uint32": 2, "int32": 2, "float32": 2, "float64": 4}[
                p.data_type
            ]
            addresses = {(p.device_id, p.register_kind, p.address + i) for i in range(width)}
            if occupied & addresses or p.address + width > 65536:
                raise ValueError("overlapping/out of range simulation registers")
            occupied |= addresses
        elif not p.node_id.startswith("ns=2;s="):
            raise ValueError("simulation NodeId must use ns=2;s=<name>")
    return config  # the storage guards at run time decide what fits; `estimate_budget` only informs (research_lab, status)
