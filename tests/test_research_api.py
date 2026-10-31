import pytest
from fastapi.testclient import TestClient
from owner_support import small_config
from test_archive_router import Disks

from htsg.archive_router import ArchiveRouter
from htsg.auth import Accounts
from htsg.config import Settings, load_settings
from htsg.owner import Owner
from htsg.web import create_web_app


def test_research_configuration_is_replaceable_and_model_references_are_checked():
    config = load_settings("config/research-sim.json")
    assert len(config.ui.stations) == 8
    assert {s.kind for s in config.sources} == {"modbus", "opcua"}
    edited = config.model_dump()
    next(d for d in edited["derived"] if d["operation"] == "thermal_stress")["model_id"] = "missing"
    with pytest.raises(ValueError, match="thermal_stress needs"):
        Settings.model_validate(edited)


def test_real_account_changes_are_audited_and_viewer_cannot_control(archive_db_factory, tmp_path):
    config = small_config()
    router = ArchiveRouter(config, archive_db_factory("primary"), archive_db_factory("emergency"), disk_usage=Disks())
    router.initialize()
    accounts = Accounts(tmp_path / "accounts.json")
    admin = {"Authorization": "Bearer " + accounts.create("admin", "admin")}
    viewer = {"Authorization": "Bearer " + accounts.create("viewer", "viewer")}
    accounts.record = router.primary.audit  # what the runtime does at start
    app = create_web_app(config=config, accounts=accounts, owner=Owner(config, 1000.0, 100.0, run_id="t"), archive_router=router)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/me", headers=viewer).json()["role"] == "viewer"
        assert client.get("/api/ui-config").status_code == 401
        assert "config_id" in client.get("/api/ui-config", headers=viewer).json()
        assert client.get("/api/admin/audit", headers=viewer).status_code == 403
        assert client.post("/api/admin/accounts", headers=viewer, json={"username": "x", "role": "viewer"}).status_code == 403
        result = client.post("/api/admin/accounts", headers=admin, json={"username": "new", "role": "viewer"})
        assert result.status_code == 201
        assert client.delete("/api/admin/accounts/new", headers=admin).status_code == 200
        log = client.get("/api/admin/audit", headers=admin).json()["items"]
        assert {x["action"] for x in log} == {"CREATE_ACCOUNT", "REVOKE_ACCOUNT"}
        assert all(x["actor"] == "admin" and x["target"] == "new" and x["result"] == "COMMITTED" for x in log)
        assert "credential" not in str(log)
