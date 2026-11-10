import asyncio
import json
import socket
import time
from pathlib import Path

import pytest
from asyncua import Client
from fastapi.testclient import TestClient
from fault_support import Accepting, faults_of
from owner_support import FixedBucket, Recorder, notify, poll
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
    ids = {"bridge_mvv_01", "temp_wall_001"}
    data["points"] = [p for p in data["points"] if p["id"] in ids]
    data["derived"] = []
    data["ui"] = {}
    data["simulation"]["signals"] = [
        {"point": "bridge_mvv_01", "base": 0.708},
        {"point": "temp_wall_001", "base": 125},
    ]
    data["points"][1].update(scale=2, offset=5)
    data["simulation"]["scenarios"] = [
        {"name": "baseline", "label": "Baseline", "kind": "baseline"},
        {
            "name": "step",
            "label": "Step",
            "kind": "thermal_step",
            "duration": 30,
            "offsets": {"temp_wall_001": 80},
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
    assert len(config.points) == 230 and len(config.ui.stations) == 8  # A1
    assert len([p for p in config.points if p.source == "ua_field"]) == 222
    assert len(config.derived) == 25
    assert len([p for p in config.points if p.source == "mb_field"]) == 8
    assert all(p.unit == "mV/V" for p in config.points if p.source == "mb_field")
    assert budget["validation_bytes_estimate"] < 2 * 1024**3
    # by hand: 2100 s x ((255 stored + 230 raw) x 1024 B) + 8 MiB; it followed the channel count down from 417 + 208
    assert budget["validation_bytes_estimate"] == 2100 * (255 + 230) * 1024 + 8 * 1024**2


def test_the_estimate_of_a_validation_run_never_decides_whether_a_configuration_loads():
    """SIM-01: the old per-frame estimate (1 KiB a value) refused the default research configuration outright once the budget was
    1 GiB or the duration 16,765 s - while the real guards, at run time, count what is actually written."""
    original = json.loads(Path("config/research-sim.json").read_text(encoding="utf-8"))
    # 修复轮18: at 255 channels the default run is estimated just under 1 GiB (1,051,332,608 B), so the budgets below it are what
    # shows the rule now: an estimate over the budget is still made, and still refuses nothing
    assert estimate_budget(Settings.model_validate(original))["validation_bytes_estimate"] > 10**9
    for budget, seconds in ((10**9, 2100), (1024, 2100), (10 * 1024**3, 30000), (10 * 1024**3, 86400)):
        data = json.loads(json.dumps(original))
        data["storage"]["development_budget_bytes"], data["simulation"]["validation_duration_seconds"] = budget, seconds
        config = Settings.model_validate(data)
        assert estimate_budget(config)["validation_bytes_estimate"] > budget, "the estimate is still made, and says it does not fit"


def test_simulation_refuses_invalid_references_and_nonlocal_servers():
    original = json.loads(Path("config/research-sim.json").read_text(encoding="utf-8"))
    for mutate in (
        lambda d: d["sources"][0].update(host="0.0.0.0"),
        lambda d: d["simulation"]["signals"].pop(),
        lambda d: d["ui"]["stations"][0].update(strain="unknown"),
        lambda d: d["sources"][0].update(interval=0.02),
        lambda d: d["points"][3].update(address=0),
        lambda d: (lambda ua: ua[1].update(node_id=ua[0]["node_id"]))([p for p in d["points"] if "node_id" in p]),  # two OPC UA points, one node
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
    assert field.values["temp_wall_001"]["value"] == 205
    field.scenario_started -= 31
    field.generate(1041)
    assert field.scenario.name == "baseline"
    assert field.values["temp_wall_001"]["value"] == 125


def test_freeze_preserves_source_timestamp_not_receipt_status():
    field = FieldSimulator(small_config(), write_ceiling=True)
    field.generate(1000)
    field.set_scenario("freeze")
    field.generate(1001)
    assert field.values["temp_wall_001"]["source_time"] == 1000
    assert field.values["temp_wall_001"]["quality"] == "GOOD"
    field.set_scenario("baseline")
    field.generate(1002)
    assert field.values["temp_wall_001"]["source_time"] == 1002


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
            node = ua.get_node("ns=2;s=temp_wall_001")
            data = await node.read_data_value()
            assert data.Value.Value == 60 and data.StatusCode.is_good()
            assert (
                abs(data.SourceTimestamp.timestamp() - field.values["temp_wall_001"]["source_time"]) < 0.001
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
        ("modbus", "mb_off", "mb_field", "bridge_mvv_01", "temp_wall_001"),
        ("opcua", "ua_off", "ua_field", "temp_wall_001", "bridge_mvv_01"),
    ],
)
async def test_backend_active_reads_both_real_protocols_and_source_restart(kind, scenario, source_id, point, other):
    config = small_config()
    field = FieldSimulator(config, write_ceiling=True)
    await field.start()
    try:
        async with Chain(config) as chain:
            await chain.until(lambda: chain.view("bridge_mvv_01")["valid"] and chain.view("temp_wall_001")["valid"], "both protocols read")
            assert chain.view("bridge_mvv_01")["value"] == pytest.approx(0.708)
            assert chain.view("temp_wall_001")["value"] == 125
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


async def test_full_owner_capacity_has_eight_bridge_and_222_opcua_inputs():
    """I1 with the simulator's own servers, H4: what goes out over the wire is the point table, nothing of the model inside."""
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

            await chain.until(lambda: len(counts()) == 230 and min(counts().values()) >= 2, "every input delivered twice", timeout=30)
            assert set(field.nodes) == {p.id for p in config.points if p.source == "ua_field"}
            assert len(field.nodes) == 222
            assert set(counts()) == {p.id for p in config.points}
            assert {o.quality for o in chain.recorder.observations()} == {"GOOD"}
            assert chain.owner.health["resyncs"] == 0 and chain.owner.health["rejected_items"] == 0
    finally:
        await field.stop()


# ---------------------------------------------------------------- the low-order unit model (修复轮15, 审核 2.6)

RESEARCH = load_settings("config/research-sim.json")
LIMITS = {p.id: p for p in RESEARCH.points}
WALLS = range(1, 216)
BRIDGE_IDS = [p.id for p in RESEARCH.points if p.source == "mb_field"]


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
    """The inputs beyond their ordinary limits and the strains beyond +-690 με."""
    out = {
        p
        for p, v in row.items()
        if (LIMITS[p].high is not None and v > LIMITS[p].high) or (LIMITS[p].low is not None and v < LIMITS[p].low)
    }
    return out | {f"strain_{n:02d}" for n in range(1, 9) if abs(row[f"bridge_mvv_{n:02d}"] * 1000 / 6) > 690}


class Monitored:
    """The simulated field read by the real owner of the shipped configuration, a second at a time: only what changed goes over
    OPC UA, a round goes over Modbus, the clock ticks.  What the field does is judged the way the system judges it - which is
    what "two records" in the manual means, and which a look at the values alone cannot show."""

    def __init__(self, clock, field):
        self.clock, self.field, self.sink, self.sent, self.step = clock, field, Accepting(), {}, 0
        self.owner = Owner(RESEARCH, 1000.0, 100.0, commit=self.sink, buckets=FixedBucket(), run_id="sim")
        self.sink.owner = self.owner
        for source in ("mb_field", "ua_field"):
            self.owner.begin_generation(source, 1, 1000.0, 100.0)

    def run(self, seconds):
        rows = []
        for _ in range(int(seconds)):
            self.step += 1
            wall, mono = 1000.0 + self.step, 100.0 + self.step
            self.field.generate(wall)
            self.clock.now += 1
            row = {k: v["value"] for k, v in self.field.values.items()}
            rows.append(row)
            changed = [(p, v) for p, v in row.items() if p not in BRIDGE_IDS and self.sent.get(p) != v]
            self.sent |= dict(changed)
            self.owner.accept(notify(changed, source="ua_field", wall=wall, mono=mono, seq=self.step))
            self.owner.accept(poll(self.step, {b: row[b] for b in BRIDGE_IDS}, source="mb_field", wall=wall, mono=mono))
            self.owner.tick(wall, mono)
        return rows

    def records(self):
        return sorted((f.point_id, f.direction) for f in faults_of(self.sink))


def test_what_the_field_sends_is_the_point_table_and_nothing_of_the_model_inside():
    """H4 (用户 11-05): the wall drop and the main steam pressure move the strain inside the model, and do not leave it.  A value
    with no point in the table would be an unknown item the moment a real client read it."""
    field = FieldSimulator(RESEARCH)
    field.generate(1000)
    assert set(field.values) == {p.id for p in RESEARCH.points}
    for name in field.values:
        assert not name.startswith(("temp_inner", "temp_outer", "wall_drop", "delta_t")) and name not in ("main_steam_pressure", "waterwall_flow")


@pytest.mark.parametrize("steady", [False, True], ids=["demonstration", "steady"])
def test_the_plant_at_its_base_swing_crosses_no_limit_and_the_walls_follow_the_heat(monkeypatch, steady):
    """H1: two hours of the base swing, at either pace: the field alarms only in a scenario.  The burner zone is hotter than the
    bottom of the furnace (修复轮15); the limit of 490 is close (默认越限阈值与回差 §6: base at most 486.3)."""
    clock, field = Clock(monkeypatch), FieldSimulator(RESEARCH, steady=steady)
    rows = clock.run(field, 7200)
    assert not set().union(*map(crossed, rows))
    last = rows[-1]
    assert last["temp_wall_003"] > last["temp_wall_001"], "follows the local heat"
    assert max(r[f"temp_wall_{n:03d}"] for r in rows for n in WALLS) <= 486.3
    loads = [r["load_mw"] for r in rows]
    assert 580 < min(loads) and max(loads) < 620 and max(loads) - min(loads) > 10, "a slow swing, not a flat line"
    assert all(round(v, 1) == v for r in rows for p, v in r.items() if p.startswith("temp_")), "0.1 degC, as a DCS shows"
    changes = {p: sum(a[p] != b[p] for a, b in zip(rows, rows[1:])) for p in rows[0]}
    walls = [p for p in changes if p.startswith("temp_wall_")]
    wall_seconds = len(rows) / (sum(changes[p] for p in walls) / len(walls))  # seconds between two changes of a wall
    if steady:
        # temperature and strain are slow quantities (用户 11-01): the steady field, the one the tests run on, holds a wall
        # reading for tens of seconds and sends a few changes a second, not 230
        assert wall_seconds > 30
        assert len(rows) / (sum(changes[p] for p in changes if p.startswith("bridge_")) / 8) > 20
        assert sum(changes.values()) / len(rows) < 10
    else:
        # the demonstration field moves visibly: a wall changes every few seconds, still far below every input every second
        assert wall_seconds < 5
        assert 30 < sum(changes.values()) / len(rows) < 150


def test_the_base_swing_read_by_the_real_owner_raises_no_record(monkeypatch):
    """H1 as the system judges it: a noise spike over a limit would be a record however briefly it lasted on the chart."""
    monitored = Monitored(Clock(monkeypatch), FieldSimulator(RESEARCH))
    monitored.run(1800)
    assert monitored.records() == []


def test_a_load_ramp_carries_flows_and_walls_along_smoothly(monkeypatch):
    """变负荷 is the main demonstration: the load goes down at 12 MW/min, is held and comes back, and everything follows
    it through its own lag - nothing jumps and nothing alarms."""
    clock, field = Clock(monkeypatch), FieldSimulator(RESEARCH)
    before = clock.run(field, 300)[-1]
    field.set_scenario("load_ramp")
    rows = clock.run(field, 1200)
    held = rows[700]  # about 450 s down; the hold ends near 750 s
    assert held["load_mw"] == pytest.approx(510, abs=2)
    assert held["main_steam_flow"] == pytest.approx(2117 * 510 / 600, rel=0.01)
    assert held["feedwater_flow"] == pytest.approx(1850 * 510 / 600, rel=0.01), "the flow that was the waterwall flow, still with the load"
    for n in (3, 4):  # the burner zone: less heat at less load
        assert held[f"temp_wall_{n:03d}"] < before[f"temp_wall_{n:03d}"]
    assert max(abs(b["load_mw"] - a["load_mw"]) for a, b in zip(rows, rows[1:])) < 1.5, "0.2 MW a second, no jump"
    assert max(abs(b["temp_wall_003"] - a["temp_wall_003"]) for a, b in zip(rows, rows[1:])) < 1
    assert not set().union(*map(crossed, rows))
    after = clock.run(field, 600)
    assert field.scenario.name == "baseline" and 585 < after[-1]["load_mw"] < 615


def test_a_hotspot_drives_its_wall_and_strain_across_their_limits_and_leaves_the_others_alone(monkeypatch):
    """H2 on the values: more heat at wall 001 raises it past its limit and pulls strain 01 past its own; a twin field without the
    hotspot shows that nothing else moves; after the scenario both come back inside."""
    clock, field, twin = Clock(monkeypatch), FieldSimulator(RESEARCH), FieldSimulator(RESEARCH)
    own = {"temp_wall_001", "bridge_mvv_01"}
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
    assert crossed(peak) == {"temp_wall_001", "strain_01"}
    first = next(i for i, r in enumerate(hot) if crossed(r))
    assert 60 < first < 300, "the walls warm through their lag; the alarm is not instant"
    after = clock.run(field, 600)
    assert field.scenario.name == "baseline" and not crossed(after[-1])


def test_a_hotspot_is_two_records_on_the_real_owner_and_both_recover(monkeypatch):
    """H2, 修复轮18 §3: 局部超温 is two records, the wall high and the strain low; the manual tells the operator to expect them."""
    clock = Clock(monkeypatch)
    monitored = Monitored(clock, FieldSimulator(RESEARCH))
    monitored.run(120)
    monitored.field.set_scenario("hotspot_01")
    monitored.run(600)
    assert monitored.records() == [("strain_01", "LOW"), ("temp_wall_001", "HIGH")]
    assert monitored.owner.tracker.confirmed_direction("temp_wall_001") == "HIGH"
    monitored.run(600)
    assert monitored.field.scenario.name == "baseline"
    assert monitored.owner.tracker.confirmed_direction("temp_wall_001") is None, "it recovers"
    assert monitored.owner.tracker.confirmed_direction("strain_01") is None, "it recovers"
    assert monitored.records() == [("strain_01", "LOW"), ("temp_wall_001", "HIGH")], "still two, nothing else on the way back"


def test_overload_is_one_record_of_the_steam_flow_and_stays_below_the_load_limit(monkeypatch):
    """H3, 修复轮18 §3: 超负荷 at 690 MW crosses the main steam flow only; the load stays below its 693 MW limit and the walls below
    490 degC, and the pressure limit that used to fire with it is gone."""
    clock = Clock(monkeypatch)
    monitored = Monitored(clock, FieldSimulator(RESEARCH))
    monitored.run(120)
    monitored.field.set_scenario("overload")
    rows = monitored.run(1200)
    assert set().union(*map(crossed, rows)) == {"main_steam_flow"}
    assert max(r["load_mw"] for r in rows) < 693
    assert monitored.records() == [("main_steam_flow", "HIGH")]


def test_the_extreme_temperature_scenario_is_bad_data_and_raises_no_record(monkeypatch):
    """H5, 默认越限阈值与回差 §5: wall 001 beyond 800 is marked extreme, and an extreme value is never a process fault."""
    clock = Clock(monkeypatch)
    monitored = Monitored(clock, FieldSimulator(RESEARCH))
    monitored.run(60)
    monitored.field.set_scenario("extreme_temperature")
    rows = monitored.run(15)
    assert max(r["temp_wall_001"] for r in rows) > 800
    view = monitored.owner.channel_views()["temp_wall_001"]
    assert view["extreme"] and not view["valid"]
    monitored.run(60)
    assert monitored.field.scenario.name == "baseline" and not monitored.owner.channel_views()["temp_wall_001"]["extreme"]
    assert monitored.records() == []


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
