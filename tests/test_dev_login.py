from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from htsg.api import create_app
from htsg.auth import Accounts
from htsg.config import Settings
from htsg.runtime import Runtime


@pytest.mark.parametrize("enabled", [False, True])
def test_short_admin_credential_is_explicit_and_shared_by_http_and_websocket(settings, tmp_path, enabled):
    accounts = Accounts(tmp_path / "accounts.json")
    original = accounts.create("admin", "admin")
    runtime = Runtime(settings.model_copy(update={"dev_admin_login": enabled}), SimpleNamespace())
    client = TestClient(create_app(runtime, accounts))
    headers = {"Authorization": "Bearer admin:admin"}
    try:
        response = client.get("/api/me", headers=headers)
        assert response.status_code == (200 if enabled else 401)
        assert client.get("/api/me", headers={"Authorization": "Bearer " + original}).status_code == 200
        assert client.get("/api/me").status_code == 401
        if enabled:
            assert response.json() == {"username": "admin", "role": "admin"}
            assert (
                client.post(
                    "/api/development/observations", headers=headers, json={"point_id": "x", "value": 2}
                ).status_code
                == 200
            )
            with client.websocket_connect("/api/stream") as ws:
                ws.send_json({"credential": "admin:admin"})
                assert ws.receive_json()["type"] == "snapshot"
            accounts.uncertain = True
            assert client.get("/api/me", headers=headers).status_code == 401
            accounts.uncertain = False
            accounts.users["admin"]["active"] = False
            assert client.get("/api/me", headers=headers).status_code == 401
    finally:
        runtime.io.shutdown(wait=True)
        runtime.read_io.shutdown(wait=True)
        runtime.calc_io.shutdown(wait=True)


def test_production_refuses_development_login():
    with pytest.raises(ValueError, match="only allowed in development"):
        Settings(mode="production", dev_admin_login=True)
