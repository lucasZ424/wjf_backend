"""Short real-protocol/real-D-database flows; independent of the 35-minute run."""

import asyncio
import inspect
import socket
import time

from htsg.config import Settings, load_settings
from htsg.runtime import Runtime
from htsg.simulation import FieldSimulator
from htsg.storage import Router


def isolated_config(time_basis="received"):
    data = load_settings("config/research-sim.json").model_dump()
    ids = {"strain_01", "temp_inner_01", "temp_outer_01", "load_mw"}
    data["time_basis"] = time_basis
    data["points"] = [p for p in data["points"] if p["id"] in ids]
    for point in data["points"]:
        point.update(stale_after=0.5, max_age=0.2)
    data["derived"] = [d for d in data["derived"] if d["id"] in {"stress_01", "delta_t_01"}]
    data["ui"]["stations"] = data["ui"]["stations"][:1]
    data["ui"]["process_points"] = ["load_mw"]
    data["ui"]["point_labels"] = {}
    data["profiles"][0].update(pre=2, recovery=0.5, post=2)
    if time_basis == "source":
        # Modbus has no source timestamp. This flow tests UA source-time freshness,
        # so it must not claim Modbus time qualification or trigger from that point.
        data["profiles"][0]["trigger_points"] = ["temp_inner_01", "temp_outer_01"]
    data["simulation"].update(interval=0.1, validation_duration_seconds=60)
    data["simulation"]["signals"] = [s for s in data["simulation"]["signals"] if s["point"] in ids]
    for scenario in data["simulation"]["scenarios"]:
        scenario["points"] = [p for p in scenario["points"] if p in ids]
        scenario["offsets"] = {p: v for p, v in scenario["offsets"].items() if p in ids}
        scenario["duration"] = 1 if scenario["kind"] == "thermal_step" else 30
    held = [socket.socket() for _ in range(3)]
    try:
        for sock in held:
            sock.bind(("127.0.0.1", 0))
        ports = [sock.getsockname()[1] for sock in held]
    finally:
        for sock in held:
            sock.close()
    data["sources"][0].update(port=ports[0], interval=0.1, timeout=0.5)
    data["sources"][1].update(
        endpoint=f"opc.tcp://127.0.0.1:{ports[1]}/research-test/", interval=0.1, timeout=0.5
    )
    data["simulation"]["control_port"] = ports[2]
    return Settings.model_validate(data)


async def eventually(condition, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if inspect.isawaitable(value):
            value = await value
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("expected condition did not become true before timeout")


def derived_status(runtime):
    frame = runtime.engine.latest_frame
    return frame["derived_results"]["stress_01"]["status"] if frame else None


async def test_real_protocol_capture_bad_quality_and_both_disconnects(databases):
    config = isolated_config()
    field = FieldSimulator(config)
    runtime = Runtime(config, Router(config, *databases))
    await field.start()
    try:
        await runtime.start()
        await eventually(lambda: derived_status(runtime) == "OK")
        await asyncio.sleep(2.2)
        field.set_scenario("thermal_step")
        await eventually(lambda: runtime.engine.active)

        async def archived_event():
            rows = await runtime.db_call(runtime.router.merged, "events")
            return next(
                (
                    r["payload"]
                    for r in rows["items"]
                    if r["payload"]["phase"] == "CLOSED" and r["payload"]["archive_status"] == "SUBMITTED"
                ),
                None,
            )

        event = await eventually(archived_event)
        assert event["coverage"] == "OBSERVED" and event["close_reason"] == "RECOVERED"
        await eventually(lambda: not runtime.events and not runtime.capture_jobs)
        samples = await runtime.db_call(runtime.router.merged, "samples", event_id=event["id"], limit=1000)
        assert {r["payload"]["point_id"] for r in samples["items"]} == set(config.profiles[0].source_points)
        assert len({r["payload"]["id"] for r in samples["items"]}) == len(samples["items"])
        products = await runtime.db_call(runtime.router.merged, "products", event_id=event["id"])
        assert products["items"] and all(not x["results_removed"] for x in products["items"])
        gaps = await runtime.db_call(runtime.router.merged, "gaps", event_id=event["id"])
        assert not gaps["items"], "normal capture must not acquire a false loss record"
        history = await runtime.db_call(
            runtime.router.merged,
            "history",
            event_id=event["id"],
            start=event["start"],
            end=event["end"],
            limit=1000,
        )
        assert {r["point_id"] for r in history["items"]} == {
            *config.profiles[0].source_points,
            *config.profiles[0].derived_points,
        }

        field.set_scenario("bad_quality")
        await eventually(lambda: not runtime.engine.states["temp_inner_01"].available)
        await eventually(lambda: derived_status(runtime) == "NOT_AVAILABLE")
        field.set_scenario("baseline")
        await eventually(lambda: derived_status(runtime) == "OK")

        for scenario, source, affected, other in (
            ("disconnect_modbus", "mb_field", "strain_01", "temp_outer_01"),
            ("disconnect_opcua", "ua_field", "temp_inner_01", "strain_01"),
        ):
            field.set_scenario(scenario)
            await eventually(
                lambda: source in runtime.source_errors and not runtime.engine.states[affected].available
            )
            assert runtime.engine.states[other].available, "one failed transport must not disable the other"
            field.set_scenario("baseline")
            await eventually(
                lambda: source not in runtime.source_errors and runtime.engine.states[affected].available
            )
            assert runtime.engine.health.get(source) != "DISCONNECTED", (
                "recovered health must not remain stale"
            )
        assert runtime.health["ingress_dropped"] == runtime.health["archive_dropped"] == 0
        assert runtime.health["write_failures"] == 0 and not runtime.failed.is_set()
    finally:
        await runtime.stop()
        await field.stop()


async def test_source_timestamp_freeze_stales_ua_even_while_transport_reads_succeed(databases):
    config = isolated_config("source")
    field = FieldSimulator(config)
    runtime = Runtime(config, Router(config, *databases))
    await field.start()
    try:
        await runtime.start()
        await eventually(lambda: derived_status(runtime) == "OK")
        # Received-time default intentionally cannot detect a frozen valid value.
        # With source time selected, repeated identical source timestamps cannot
        # keep an old engineering observation available or the model qualified.
        field.set_scenario("freeze")
        await eventually(lambda: not runtime.engine.states["temp_inner_01"].available)
        await eventually(lambda: derived_status(runtime) == "NOT_AVAILABLE")
        assert "ua_field" not in runtime.source_errors
        assert time.time() - runtime.source_receipts["ua_field"] < 0.5
        assert runtime.engine.states["temp_outer_01"].available
        field.set_scenario("baseline")
        await eventually(lambda: derived_status(runtime) == "OK")
        assert not runtime.failed.is_set()
    finally:
        await runtime.stop()
        await field.stop()
