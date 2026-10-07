import asyncio

from fastapi.testclient import TestClient

from htsg.api import create_app
from htsg.auth import Accounts
from htsg.runtime import Runtime, Subscription
from htsg.storage import Router


def test_viewer_cannot_write_and_admin_can_inject_development_sample(settings, databases, tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    viewer = accounts.create("viewer", "viewer")
    admin = accounts.create("admin", "admin")
    runtime = Runtime(settings, Router(settings, *databases))
    with TestClient(create_app(runtime, accounts)) as client:
        assert client.get("/api/current").status_code == 401
        headers = {"Authorization": "Bearer " + viewer}
        assert client.get("/api/current", headers=headers).status_code == 200
        assert (
            client.post(
                "/api/development/observations", headers=headers, json={"point_id": "x", "value": 10}
            ).status_code
            == 403
        )
        headers = {"Authorization": "Bearer " + admin}
        assert (
            client.post(
                "/api/development/observations", headers=headers, json={"point_id": "x", "value": 10}
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/api/admin/accounts", headers=headers, json={"username": "new", "role": "viewer"}
            ).status_code
            == 201
        )
        assert client.delete("/api/admin/accounts/admin", headers=headers).status_code == 409


async def test_slow_subscriber_merges_frames_but_event_overflow_requires_resync():
    sub = Subscription(2)
    for n in range(100):
        sub.push({"type": "frame", "seq": n})
    assert (await sub.next())["seq"] == 99
    for n in range(3):
        sub.push({"type": "trigger", "seq": n})
    assert (await sub.next())["type"] == "resync_required"


async def test_slow_database_does_not_block_owner(settings, databases):
    import time

    router = Router(settings, *databases)
    real = router.write_batch

    def delayed(*args):
        time.sleep(0.25)
        return real(*args)

    router.write_batch = delayed
    runtime = Runtime(settings, router)
    await runtime.start()
    try:
        runtime.submit("x", 10)
        await asyncio.sleep(0.1)
        assert runtime.engine.faults["x"].started is not None
        await asyncio.sleep(1.3)
        assert runtime.engine.latest_frame is not None
        assert len(runtime.events) <= settings.limits.event_queue
    finally:
        await runtime.stop()


async def test_calculation_timeout_publishes_unavailable_without_blocking_detection(
    settings, databases, monkeypatch
):
    import time

    import htsg.calculation

    calculate = htsg.calculation.calculate

    def slow(*args):
        time.sleep(0.3)
        return calculate(*args)

    monkeypatch.setattr(htsg.calculation, "calculate", slow)
    settings = settings.model_copy(
        update={"limits": settings.limits.model_copy(update={"calculation_timeout": 0.02})}
    )
    runtime = Runtime(settings, Router(settings, *databases))
    await runtime.start()
    try:
        runtime.submit("x", 10)
        await asyncio.sleep(0.1)
        assert runtime.engine.faults["x"].started is not None
        await asyncio.sleep(1.3)
        result = runtime.engine.latest_frame["derived_results"]["delta"]
        assert result["value"] is None and result["reason"] == "CALCULATION_TIMEOUT"
    finally:
        await runtime.stop()


def test_websocket_authentication_snapshot_and_fault(settings, databases, tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    viewer = accounts.create("viewer", "viewer")
    admin = accounts.create("admin", "admin")
    runtime = Runtime(settings, Router(settings, *databases))
    with TestClient(create_app(runtime, accounts)) as client:
        with client.websocket_connect("/api/stream") as ws:
            ws.send_json({"credential": viewer})
            assert ws.receive_json()["type"] == "snapshot"
            response = client.post(
                "/api/development/observations",
                headers={"Authorization": "Bearer " + admin},
                json={"point_id": "x", "value": 10},
            )
            assert response.status_code == 200
            import time

            for _ in range(6):
                time.sleep(1)
                client.post(
                    "/api/development/observations",
                    headers={"Authorization": "Bearer " + admin},
                    json={"point_id": "x", "value": 10},
                )
            for _ in range(20):
                if ws.receive_json()["type"] == "fault":
                    break
            else:
                raise AssertionError("abnormal notification was not delivered")
