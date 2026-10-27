"""The simplified development login and the account state behind every credential (终版 §2.3)."""

import pytest
from fastapi.testclient import TestClient
from owner_support import small_config

from htsg.auth import Accounts
from htsg.config import Settings
from htsg.owner import Owner
from htsg.web import create_web_app


def app_for(tmp_path, **settings):
    config = small_config(**settings)
    accounts = Accounts(tmp_path / "accounts.json")
    original = accounts.create("admin", "admin")
    owner = Owner(config, 1000.0, 100.0, run_id="t")
    return create_web_app(config=config, accounts=accounts, owner=owner), accounts, original


@pytest.mark.parametrize("enabled", [False, True])
def test_short_admin_credential_is_explicit_and_shared_by_every_request(tmp_path, enabled):
    app, accounts, original = app_for(tmp_path, dev_admin_login=enabled)
    headers = {"Authorization": "Bearer admin:admin"}
    with TestClient(app) as client:
        response = client.get("/api/me", headers=headers)
        assert response.status_code == (200 if enabled else 401)
        assert client.get("/api/me", headers={"Authorization": "Bearer " + original}).status_code == 200
        assert client.get("/api/me").status_code == 401
        if enabled:
            assert response.json() == {"username": "admin", "role": "admin"}
            assert client.get("/api/snapshot", headers=headers).status_code == 200  # the live reading too
            accounts.uncertain = True
            assert client.get("/api/me", headers=headers).status_code == 401
            accounts.uncertain = False
            accounts.users["admin"]["active"] = False
            assert client.get("/api/me", headers=headers).status_code == 401


def test_production_refuses_development_login():
    with pytest.raises(ValueError, match="only allowed in development"):
        Settings(mode="production", dev_admin_login=True)
