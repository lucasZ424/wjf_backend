import asyncio
import json
import socket
import time
from pathlib import Path

import pytest
from asyncua import Client
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pymodbus.client import AsyncModbusTcpClient

from htsg.adapters import adapter, decode_registers
from htsg.config import Settings, load_settings
from htsg.research_config import estimate_budget
from htsg.simulation import FieldSimulator, create_simulation_app, encode_registers, signal_value


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def small_config():
    data = json.loads(Path("config/research-sim.json").read_text(encoding="utf-8"))
    ids = {"strain_01", "temp_inner_01"}
    data["points"] = [p for p in data["points"] if p["id"] in ids]
    data["derived"] = []
    data["profiles"] = []
    data["ui"] = {}
    data["simulation"]["signals"] = [
        {"point": "strain_01", "base": 60},
        {"point": "temp_inner_01", "base": 125},
    ]
    data["points"][0].update(scale=0.5, offset=10)
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
    data["simulation"]["interval"] = 0.05
    data["simulation"]["ledger_limit"] = 3
    return Settings.model_validate(data)


def test_research_configuration_has_bounded_full_duration_capture():
    config = load_settings("config/research-sim.json")
    budget = estimate_budget(config)
    assert len(config.points) == 31 and len(config.ui.stations) == 8
    assert budget["capture_bytes_estimate"] < config.limits.capture_budget_bytes
    assert budget["validation_bytes_estimate"] < 128 * 1024**2
    assert config.profiles[0].pre == 600 and config.profiles[0].post == 1200
    assert len(config.profiles[0].source_points) == 4


def test_simulation_refuses_invalid_references_nonlocal_servers_and_overbudget():
    original = json.loads(Path("config/research-sim.json").read_text(encoding="utf-8"))
    # Exercise refusal with a small test quota; the live lab now deliberately allows 10 GiB.
    original["storage"]["development_budget_bytes"] = 128 * 1024**2
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
    field = FieldSimulator(config)
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
    field = FieldSimulator(small_config())
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
    point = small_config().points[0].model_copy(update={"word_order": word, "byte_order": byte})
    # Engineering 60 with scale .5/offset 10 is raw 100 == IEEE754 42c80000.
    assert encode_registers(60, point) == expected
    assert decode_registers(expected, point) == (60, "GOOD")


async def test_real_protocols_fixed_values_bad_quality_and_write_rejection():
    config = small_config()
    field = FieldSimulator(config)
    field.generate(time.time())
    await field.reconcile_servers()
    client = AsyncModbusTcpClient("127.0.0.1", port=config.sources[0].port, timeout=1)
    try:
        assert await client.connect()
        response = await client.read_holding_registers(0, count=2, device_id=1)
        assert response.registers == [0x42C8, 0]
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


@pytest.mark.parametrize(
    "kind,scenario,source_id,point",
    [
        ("modbus", "mb_off", "mb_field", "strain_01"),
        ("opcua", "ua_off", "ua_field", "temp_inner_01"),
    ],
)
async def test_backend_active_reads_both_real_protocols_and_source_restart(kind, scenario, source_id, point):
    config = small_config()
    field = FieldSimulator(config)
    await field.start()
    observations = []
    errors = []
    tasks = [
        asyncio.create_task(
            adapter(
                s,
                [p for p in config.points if p.source == s.id],
                lambda *args: observations.append(args),
                lambda *args: errors.append(args),
            )
        )
        for s in config.sources
    ]
    try:
        deadline = time.monotonic() + 8
        while len({x[0] for x in observations}) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        values = {x[0]: x[1] for x in observations}
        assert values == {"strain_01": 60, "temp_inner_01": 125}
        field.set_scenario(scenario)
        deadline = time.monotonic() + 8
        while not any(x[0] == source_id for x in errors) and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert getattr(field, kind) is None
        assert getattr(field, "opcua" if kind == "modbus" else "modbus") is not None
        assert any(x[0] == source_id for x in errors)
        count = len([x for x in observations if x[0] == point])
        field.set_scenario("baseline")
        deadline = time.monotonic() + 8
        while len([x for x in observations if x[0] == point]) == count and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert len([x for x in observations if x[0] == point]) > count
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
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
