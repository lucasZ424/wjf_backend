from fastapi.testclient import TestClient

from htsg.api import create_app
from htsg.auth import Accounts
from htsg.config import Settings, load_settings
from htsg.runtime import Runtime
from htsg.storage import Router


def test_research_configuration_is_replaceable_and_model_references_are_checked():
    import pytest

    config = load_settings("config/research-sim.json")
    assert len(config.ui.stations) == 8
    assert {s.kind for s in config.sources} == {"modbus", "opcua"}
    assert not config.profiles
    edited = config.model_dump()
    next(d for d in edited["derived"] if d["operation"] == "thermal_stress")["model_id"] = "missing"
    with pytest.raises(ValueError, match="thermal_stress needs"):
        Settings.model_validate(edited)


def test_real_account_changes_are_audited_and_viewer_cannot_control(settings, databases, tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    admin = {"Authorization": "Bearer " + accounts.create("admin", "admin")}
    viewer = {"Authorization": "Bearer " + accounts.create("viewer", "viewer")}
    runtime = Runtime(settings, Router(settings, *databases))
    with TestClient(create_app(runtime, accounts)) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/me", headers=viewer).json()["role"] == "viewer"
        assert client.get("/api/ui-config").status_code == 401
        assert "config_id" in client.get("/api/ui-config", headers=viewer).json()
        assert (
            client.post("/api/simulation", headers=viewer, json={"scenario": "baseline"}).status_code == 403
        )
        assert client.get("/api/admin/audit", headers=viewer).status_code == 403
        result = client.post("/api/admin/accounts", headers=admin, json={"username": "new", "role": "viewer"})
        assert result.status_code == 201
        assert client.delete("/api/admin/accounts/new", headers=admin).status_code == 200
        log = client.get("/api/admin/audit", headers=admin).json()["items"]
        assert {x["action"] for x in log} == {"CREATE_ACCOUNT", "REVOKE_ACCOUNT"}
        assert all(x["actor"] == "admin" and x["target"] == "new" and x["result"] == "COMMITTED" for x in log)
        assert "credential" not in str(log)
        runtime.report("s", "TEST_DISCONNECT")
        runtime.submit("x", 1)
        assert "s" not in runtime.source_errors
        logs = client.get("/api/communications", headers=viewer).json()["logs"]
        assert {x["message"] for x in logs} >= {"TEST_DISCONNECT", "READING_RECEIVED"}
