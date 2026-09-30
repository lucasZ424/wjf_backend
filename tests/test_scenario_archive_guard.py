from fastapi.testclient import TestClient

from htsg.api import create_app
from htsg.auth import Accounts
from htsg.research_config import Scenario, Simulation
from htsg.runtime import Runtime
from htsg.storage import Router


def test_trial_cannot_claim_success_when_archival_is_unavailable(settings, databases, tmp_path):
    # Control must be rejected before a controller command or management DB write.
    settings = settings.model_copy(
        update={
            "simulation": Simulation(
                enabled=True, scenarios=(Scenario(name="thermal_step", label="heat", kind="thermal_step"),)
            )
        }
    )
    accounts = Accounts(tmp_path / "accounts.json")
    headers = {"Authorization": "Bearer " + accounts.create("admin", "admin")}
    runtime = Runtime(settings, Router(settings, *databases))
    with TestClient(create_app(runtime, accounts)) as client:
        runtime.accepting = False
        runtime.health["write_error"] = "DEVELOPMENT_WRITE_BUDGET_EXHAUSTED"
        result = client.post("/api/simulation", headers=headers, json={"scenario": "thermal_step"})
        assert result.status_code == 409
        assert "归档" in result.json()["detail"]
        assert client.get("/api/admin/audit", headers=headers).json()["items"] == []
