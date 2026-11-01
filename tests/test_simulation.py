import asyncio
import json
import socket
import time
from pathlib import Path

import pytest
from asyncua import Client
from fastapi.testclient import TestClient
from owner_support import Recorder
from pydantic import ValidationError
from pymodbus.client import AsyncModbusTcpClient

from htsg.config import Settings, load_settings
from htsg.ingress import Ingress
from htsg.modbus import ModbusAcquirer, decode_registers
from htsg.owner import Owner
from htsg.research_config import estimate_budget
from htsg.simulation import FieldSimulator, create_simulation_app, encode_registers, signal_value
from htsg.ua import UaAcquirer


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def small_config():
    """Two points with fixed signals.  The protocol and scenario mechanics are checked on the signal path - what the
    write-ceiling load sends - so a value read back is exactly the configured one (the plant model is checked below)."""
    data = json.loads(Path("config/research-sim.json").read_text(encoding="utf-8"))
    ids = {"bridge_mvv_01", "temp_inner_01"}
    data["points"] = [p for p in data["points"] if p["id"] in ids]
    data["derived"] = []
    data["ui"] = {}
    data["simulation"]["signals"] = [
        {"point": "bridge_mvv_01", "base": 0.708},
        {"point": "temp_inner_01", "base": 125},
    ]
    data["points"][1].update(scale=2, offset=5)
    data["simulation"]["scenarios"] = [
        {"name": "baseline", "label": "Baseline", "kind": "baseline"},
        {
            "name": "step",
            "label": "Step",
            "kind": "thermal_step",
            "duration": 30,
            "offsets": {"temp_inner_01": 80},
        },
        {"name": "bad", "label": "Bad", "kind": "bad_quality", "points": list(ids)},
        {"name": "freeze", "label": "Freeze", "kind": "freeze", "points": list(ids)},
        {"name": "mb_off", "label": "MB off", "kind": "disconnect_modbus"},
        {"name": "ua_off", "label": "UA off", "kind": "disconnect_opcua"},
    ]
    data["sources"][0]["port"] = free_port()
    data["sources"][1]["endpoint"] = f"opc.tcp://127.0.0.1:{free_port()}/htsg/"
    data["simulation"]["control_port"] = free_port()
    data["simulation"]["interval"] = 1
    data["simulation"]["ledger_limit"] = 3
    return Settings.model_validate(data)


def test_research_configuration_has_bounded_full_duration_capture():
    config = load_settings("config/research-sim.json")
    budget = estimate_budget(config)
    assert len(config.points) == 208 and len(config.ui.stations) == 8
    assert len([p for p in config.points if p.source == "ua_field"]) == 200
    assert len(config.derived) == 209
    assert len([p for p in config.points if p.source == "mb_field"]) == 8
    assert all(p.unit == "mV/V" for p in config.points if p.source == "mb_field")
    assert budget["validation_bytes_estimate"] < 2 * 1024**3


def test_the_estimate_of_a_validation_run_never_decides_whether_a_configuration_loads():
    """SIM-01: the old per-frame estimate (1 KiB a value) refused the default research configuration outright once the budget was
    1 GiB or the duration 16,765 s - while the real guards, at run time, count what is actually written."""
    original = json.loads(Path("config/research-sim.json").read_text(encoding="utf-8"))
    assert estimate_budget(Settings.model_validate(original))["validation_bytes_estimate"] > 1024**3
    for budget, seconds in ((1024**3, 2100), (1024, 2100), (10 * 1024**3, 20000), (10 * 1024**3, 86400)):
        data = json.loads(json.dumps(original))
        data["storage"]["development_budget_bytes"], data["simulation"]["validation_duration_seconds"] = budget, seconds
        config = Settings.model_validate(data)
        assert estimate_budget(config)["validation_bytes_estimate"] > budget, "the estimate is still made, and says it does not fit"


def test_simulation_refuses_invalid_references_and_nonlocal_servers():
    original = json.loads(Path("config/research-sim.json").read_text(encoding="utf-8"))
    for mutate in (
        lambda d: d["sources"][0].update(host="0.0.0.0"),
        lambda d: d["simulation"]["signals"].pop(),
        lambda d: d["ui"]["stations"][0].update(stress="unknown"),
        lambda d: d["sources"][0].update(interval=0.02),
        lambda d: d["points"][3].update(address=0),
        lambda d: d["points"][2].update(node_id=d["points"][1]["node_id"]),
    ):
        data = json.loads(json.dumps(original))
        mutate(data)
        with pytest.raises(ValidationError):
            Settings.model_validate(data)


def test_deterministic_generation_and_finite_ledger():
    config = small_config()
    field = FieldSimulator(config, write_ceiling=True)
    signal = config.simulation.signals[0].model_copy(update={"amplitude": 10, "noise": 1})
    assert signal_value(signal, 42, 50, 1) == signal_value(signal, 42, 50, 1)
    assert signal_value(signal, 42, 50, 1) != signal_value(signal, 43, 50, 1)
    for i in range(10):
        field.generate(1000 + i)
    assert len(field.ledger) == 3
    assert field.ledger[0]["step"] == 7
    field.set_scenario("step")
    field.generate(1010)
    assert field.values["temp_inner_01"]["value"] == 205
    field.scenario_started -= 31
    field.generate(1041)
    assert field.scenario.name == "baseline"
    assert field.values["temp_inner_01"]["value"] == 125


def test_freeze_preserves_source_timestamp_not_receipt_status():
    field = FieldSimulator(small_config(), write_ceiling=True)
    field.generate(1000)
    field.set_scenario("freeze")
    field.generate(1001)
    assert field.values["temp_inner_01"]["source_time"] == 1000
    assert field.values["temp_inner_01"]["quality"] == "GOOD"
    field.set_scenario("baseline")
    field.generate(1002)
    assert field.values["temp_inner_01"]["source_time"] == 1002


@pytest.mark.parametrize(
    "word,byte,expected",
    [
        ("big", "big", [0x42C8, 0]),
        ("little", "big", [0, 0x42C8]),
        ("big", "little", [0xC842, 0]),
        ("little", "little", [0, 0xC842]),
    ],
)
def test_modbus_wire_encoding_against_independent_ieee754_fixture(word, byte, expected):
    point = (
        small_config()
        .points[0]
        .model_copy(update={"word_order": word, "byte_order": byte, "scale": 0.5, "offset": 10})
    )
    # Engineering 60 with scale .5/offset 10 is raw 100 == IEEE754 42c80000.
    assert encode_registers(60, point) == expected
    assert decode_registers(expected, point) == (60, "GOOD")


async def test_real_protocols_fixed_values_bad_quality_and_write_rejection():
    config = small_config()
    field = FieldSimulator(config, write_ceiling=True)
    field.generate(time.time())
    await field.reconcile_servers()
    client = AsyncModbusTcpClient("127.0.0.1", port=config.sources[0].port, timeout=1)
    try:
        assert await client.connect()
        response = await client.read_holding_registers(0, count=2, device_id=1)
        assert response.registers == encode_registers(0.708, config.points[0])
        rejected = await client.write_register(0, 0, device_id=1)
        assert rejected.isError()
        async with Client(config.sources[1].endpoint) as ua:
            node = ua.get_node("ns=2;s=temp_inner_01")
            data = await node.read_data_value()
            assert data.Value.Value == 60 and data.StatusCode.is_good()
            assert (
                abs(data.SourceTimestamp.timestamp() - field.values["temp_inner_01"]["source_time"]) < 0.001
            )
            field.set_scenario("bad")
            field.generate(time.time())
            await field.write_opcua()
            data = await node.read_data_value(raise_on_bad_status=False)
            assert data.StatusCode.is_bad()
            response = await client.read_holding_registers(0, count=2, device_id=1)
            assert decode_registers(response.registers, config.points[0]) == (None, "BAD")
    finally:
        client.close()
        await field.stop()


class Chain:
    """The real acquirers feeding a real owner through the ordered ingress - what the runtime assembles, without a database."""

    def __init__(self, config):
        self.config, self.recorder = config, Recorder()
        self.owner = Owner(config, time.time(), time.monotonic(), commit=self.recorder.commit, run_id="sim")
        self.ingress = Ingress(self.owner, 4096)
        self.acquirers = [
            (ModbusAcquirer if s.kind == "modbus" else UaAcquirer)(s, [p for p in config.points if p.source == s.id], config.timing, self.ingress)
            for s in config.sources
        ]
        self.owner.on_rebuild = lambda source_id, reason: [a.request_rebuild(source_id, reason) for a in self.acquirers if hasattr(a, "request_rebuild")]
        self.tasks = []

    async def pump(self):
        next_tick = time.monotonic() + 1
        while True:
            self.ingress.drain()
            now = time.monotonic()
            if now >= next_tick:
                self.owner.tick(time.time(), now)
                next_tick += 1
            await asyncio.sleep(0.05)

    async def __aenter__(self):
        self.tasks = [asyncio.create_task(a.run()) for a in self.acquirers] + [asyncio.create_task(self.pump())]
        return self

    async def __aexit__(self, *exc):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def view(self, point):
        return self.owner.channel_views()[point]

    async def until(self, condition, why, timeout=20):
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.05)
        return why


@pytest.mark.parametrize(
    "kind,scenario,source_id,point,other",
    [
        ("modbus", "mb_off", "mb_field", "bridge_mvv_01", "temp_inner_01"),
        ("opcua", "ua_off", "ua_field", "temp_inner_01", "bridge_mvv_01"),
    ],
)
async def test_backend_active_reads_both_real_protocols_and_source_restart(kind, scenario, source_id, point, other):
    config = small_config()
    field = FieldSimulator(config, write_ceiling=True)
    await field.start()
    try:
        async with Chain(config) as chain:
            await chain.until(lambda: chain.view("bridge_mvv_01")["valid"] and chain.view("temp_inner_01")["valid"], "both protocols read")
            assert chain.view("bridge_mvv_01")["value"] == pytest.approx(0.708)
            assert chain.view("temp_inner_01")["value"] == 125
            field.set_scenario(scenario)
            await chain.until(lambda: not chain.view(point)["valid"] and getattr(field, kind) is None, "the stopped source is seen as down")
            assert getattr(field, "opcua" if kind == "modbus" else "modbus") is not None
            assert chain.view(other)["valid"], "the other protocol is not disturbed"
            seen = len(chain.recorder.observations(point))
            field.set_scenario("baseline")
            await chain.until(lambda: chain.view(point)["valid"] and len(chain.recorder.observations(point)) > seen, "the restarted source delivers again")
    finally:
        await field.stop()


def test_control_requires_secret_and_rejects_unknown_scenario(monkeypatch):
    config = small_config()
    monkeypatch.setenv(config.simulation.token_env, "test-simulator-control-secret")
    client = TestClient(create_simulation_app(config))
    assert client.get("/status").status_code == 401
    headers = {"Authorization": "Bearer test-simulator-control-secret"}
    assert client.post("/scenario", json={"scenario": "missing"}, headers=headers).status_code == 422
    response = client.post("/scenario", json={"scenario": "step"}, headers=headers)
    assert response.status_code == 200 and response.json()["scenario"] == "step"
    assert "test-simulator-control-secret" not in response.text


async def test_full_owner_capacity_has_eight_bridge_and_two_hundred_opcua_inputs():
    data = load_settings("config/research-sim.json").model_dump()
    data["sources"][0]["port"] = free_port()
    data["sources"][1]["endpoint"] = f"opc.tcp://127.0.0.1:{free_port()}/full-capacity/"
    data["simulation"]["control_port"] = free_port()
    config = Settings.model_validate(data)
    field = FieldSimulator(config, write_ceiling=True)  # every input changes every second: the full load
    try:
        await field.start()
        async with Chain(config) as chain:
            def counts():
                found = {}
                for o in chain.recorder.observations():
                    found[o.point_id] = found.get(o.point_id, 0) + 1
                return found

            await chain.until(lambda: len(counts()) == 208 and min(counts().values()) >= 2, "every input delivered twice", timeout=30)
            assert len(field.nodes) == 200
            assert set(counts()) == {p.id for p in config.points}
            assert {o.quality for o in chain.recorder.observations()} == {"GOOD"}
            assert chain.owner.health["resyncs"] == 0 and chain.owner.health["rejected_items"] == 0
    finally:
        await field.stop()


# ---------------------------------------------------------------- the low-order unit model (修复轮15, 审核 2.6)

RESEARCH = load_settings("config/research-sim.json")
LIMITS = {p.id: p for p in RESEARCH.points}
PAIRS = range(1, 97)


class Clock:
    """The simulator measures a scenario's age on the monotonic clock; here a second passes per generated step."""

    def __init__(self, monkeypatch):
        self.now = 0.0
        monkeypatch.setattr("htsg.simulation.time.monotonic", lambda: self.now)

    def run(self, field, seconds):
        rows = []
        for _ in range(int(seconds)):
            field.generate(1000 + field.step)
            self.now += 1
            rows.append({k: v["value"] for k, v in field.values.items()})
        return rows


def crossed(row):
    """The inputs beyond their ordinary limits, the wall differences beyond +-55 K and the strains beyond +-690 με."""
    out = {
        p
        for p, v in row.items()
        if (LIMITS[p].high is not None and v > LIMITS[p].high) or (LIMITS[p].low is not None and v < LIMITS[p].low)
    }
    out |= {f"delta_t_{n:02d}" for n in PAIRS if abs(row[f"temp_inner_{n:02d}"] - row[f"temp_outer_{n:02d}"]) > 55}
    return out | {f"strain_{n:02d}" for n in range(1, 9) if abs(row[f"bridge_mvv_{n:02d}"] * 1000 / 6) > 690}


def wall_difference(row, n):
    return row[f"temp_inner_{n:02d}"] - row[f"temp_outer_{n:02d}"]


@pytest.mark.parametrize("steady", [False, True], ids=["demonstration", "steady"])
def test_the_plant_at_its_base_swing_crosses_no_limit_and_heats_the_tubes_from_outside(monkeypatch, steady):
    """Two hours of the base swing, at either pace: the field alarms only in a scenario, and every wall pair is a heated
    waterwall tube (outer wall hotter), with a burner zone hotter than the bottom of the furnace."""
    clock, field = Clock(monkeypatch), FieldSimulator(RESEARCH, steady=steady)
    rows = clock.run(field, 7200)
    assert not set().union(*map(crossed, rows))
    assert all(wall_difference(r, n) < 0 for r in rows for n in PAIRS)
    last = rows[-1]
    assert -35 < wall_difference(last, 3) < -25 and -20 < wall_difference(last, 1) < -12, "follows the local heat"
    loads = [r["load_mw"] for r in rows]
    assert 580 < min(loads) and max(loads) < 620 and max(loads) - min(loads) > 10, "a slow swing, not a flat line"
    assert all(round(v, 1) == v for r in rows for p, v in r.items() if p.startswith("temp_")), "0.1 degC, as a DCS shows"
    changes = {p: sum(a[p] != b[p] for a, b in zip(rows, rows[1:])) for p in rows[0]}
    walls = [p for p in changes if p.startswith("temp_")]
    wall_seconds = len(rows) / (sum(changes[p] for p in walls) / len(walls))  # seconds between two changes of a wall
    if steady:
        # temperature and strain are slow quantities (用户 11-01): the steady field, the one the tests run on, holds a wall
        # reading for tens of seconds and sends a few changes a second, not 208
        assert wall_seconds > 30
        assert len(rows) / (sum(changes[p] for p in changes if p.startswith("bridge_")) / 8) > 20
        assert sum(changes.values()) / len(rows) < 10
    else:
        # the demonstration field moves visibly: a wall changes every few seconds, still far below every input every second
        assert wall_seconds < 5
        assert 30 < sum(changes.values()) / len(rows) < 150


def test_a_load_ramp_carries_flows_pressure_and_wall_differences_along_smoothly(monkeypatch):
    """变负荷 is the main demonstration: the load goes down at 12 MW/min, is held and comes back, and everything follows
    it through its own lag - nothing jumps and nothing alarms."""
    clock, field = Clock(monkeypatch), FieldSimulator(RESEARCH)
    before = clock.run(field, 300)[-1]
    field.set_scenario("load_ramp")
    rows = clock.run(field, 1200)
    held = rows[700]  # about 450 s down; the hold ends near 750 s
    assert held["load_mw"] == pytest.approx(510, abs=2)
    assert held["main_steam_flow"] == pytest.approx(2117 * 510 / 600, rel=0.01)
    assert held["main_steam_pressure"] < before["main_steam_pressure"] - 1.2, "sliding pressure, behind its lag"
    for n in (3, 4):  # the burner zone: less heat, a smaller wall difference and so a smaller stress proxy
        assert wall_difference(held, n) > wall_difference(before, n) + 3
    assert max(abs(b["load_mw"] - a["load_mw"]) for a, b in zip(rows, rows[1:])) < 1.5, "0.2 MW a second, no jump"
    assert max(abs(b["temp_outer_03"] - a["temp_outer_03"]) for a, b in zip(rows, rows[1:])) < 1
    assert not set().union(*map(crossed, rows))
    after = clock.run(field, 600)
    assert field.scenario.name == "baseline" and 585 < after[-1]["load_mw"] < 615


def test_a_hotspot_drives_its_station_across_its_limits_and_leaves_the_others_alone(monkeypatch):
    """测点01局部超温: more heat at pair 01 raises the outer wall more than the inner one, so the outer wall, the wall
    difference (negative side, LOW) and the strain cross their limits; a twin field without the hotspot shows that
    nothing else moves; after the scenario every one of them comes back inside."""
    clock, field, twin = Clock(monkeypatch), FieldSimulator(RESEARCH), FieldSimulator(RESEARCH)
    own = {"temp_inner_01", "temp_outer_01", "bridge_mvv_01"}
    for f in (field, twin):
        for _ in range(120):
            f.generate(1000 + f.step)
    field.set_scenario("hotspot_01")
    hot = []
    for _ in range(600):
        field.generate(1000 + field.step)
        twin.generate(1000 + twin.step)
        clock.now += 1
        hot.append({k: v["value"] for k, v in field.values.items()})
        others = {k: v["value"] for k, v in field.values.items() if k not in own}
        assert others == {k: v["value"] for k, v in twin.values.items() if k not in own}
    peak = hot[-1]
    assert crossed(peak) == {"temp_outer_01", "delta_t_01", "strain_01"}
    # the outer wall gains the film and the wall terms (70 K per unit heat), the inner one the film term only (40 K)
    assert peak["temp_outer_01"] - hot[0]["temp_outer_01"] > 1.5 * (peak["temp_inner_01"] - hot[0]["temp_inner_01"])
    first = next(i for i, r in enumerate(hot) if crossed(r))
    assert 60 < first < 300, "the walls warm through their lag; the alarm is not instant"
    after = clock.run(field, 600)
    assert field.scenario.name == "baseline" and not crossed(after[-1])


def test_overload_crosses_pressure_and_steam_flow_but_not_the_load_limit(monkeypatch):
    """超负荷 is named for what it does: at 690 MW the main steam pressure and flow cross their limits; the load stays
    below its 693 MW limit and the walls below 490 degC."""
    clock, field = Clock(monkeypatch), FieldSimulator(RESEARCH)
    clock.run(field, 120)
    field.set_scenario("overload")
    rows = clock.run(field, 1200)
    assert set().union(*map(crossed, rows)) == {"main_steam_pressure", "main_steam_flow"}
    assert max(r["load_mw"] for r in rows) < 693


def test_the_write_ceiling_changes_every_input_every_second_and_refuses_process_scenarios(monkeypatch):
    """The most a gateway sending raw noisy values could make the backend write stays available to the development load
    tests (审核 2.6, 用户 11-01: not a field mode); the plant model writes far less."""
    clock, field = Clock(monkeypatch), FieldSimulator(RESEARCH, write_ceiling=True)
    assert FieldSimulator(RESEARCH, steady=True).status()["mode"] == "steady"
    assert FieldSimulator(RESEARCH).status()["mode"] == "demonstration", "the default is the field to show"
    rows = clock.run(field, 5)
    assert all(a[p] != b[p] for a, b in zip(rows, rows[1:]) for p in a)
    for name in ("load_ramp", "hotspot_01", "overload"):
        with pytest.raises(ValueError):
            field.set_scenario(name)
    field.set_scenario("freeze")
    assert field.status()["mode"] == "write_ceiling"
