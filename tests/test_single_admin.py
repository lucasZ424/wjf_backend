"""One administrator per deployment, made by the deployment itself (修复轮02, user ruling 2026-10-26).

Why it matters: a deployment must never reach its first start with no account at all - the state in which nobody can
sign in, or whoever reaches the login page first could make themselves administrator.  So the deployment writes the only
administrator into the primary database and the account file together; the pages only add read-only users, and the
administrator may change their own password there at any time.
"""

import json

import pytest
from fastapi.testclient import TestClient
from owner_support import small_config

from htsg import cli
from htsg.auth import Accounts
from htsg.owner import Owner
from htsg.web import create_web_app

NEW = "a-password-of-my-own"


def test_there_is_only_one_administrator(tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    accounts.create("admin", "admin")
    with pytest.raises(ValueError, match="已有管理员"):
        accounts.create("second", "admin")
    assert accounts.create("operator", "viewer").startswith("operator:"), "read-only users are added freely"
    assert [name for name, user in accounts.users.items() if user["role"] == "admin"] == ["admin"]


def test_the_administrator_sets_a_password_of_their_own_and_the_old_credential_stops_working(tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    old = accounts.create("admin", "admin")
    records = []
    accounts.record = records.append
    accounts.set_secret("admin", NEW, {"action": "SET_PASSWORD"})
    assert accounts.verify(old) is None and accounts.verify("admin:" + NEW)["role"] == "admin"
    assert records == [{"action": "SET_PASSWORD"}], "on record"
    assert Accounts(accounts.path).verify("admin:" + NEW), "and in the account file"


@pytest.mark.parametrize("secret", ["short", " padded password ", "x" * 251], ids=["too-short", "padded", "too-long"])
def test_a_password_too_short_or_one_the_login_could_never_take_is_refused(tmp_path, secret):
    """The login trims what is typed and takes at most 256 characters for `admin:password`."""
    accounts = Accounts(tmp_path / "accounts.json")
    old = accounts.create("admin", "admin")
    with pytest.raises(ValueError):
        accounts.set_secret("admin", secret)
    assert accounts.verify(old), "nothing changed"


def test_through_the_api_only_read_only_users_are_added_and_only_the_administrator_changes_a_password(tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    admin, viewer = accounts.create("admin", "admin"), accounts.create("viewer", "viewer")
    owner = Owner(small_config(), 1000.0, 100.0, run_id="t")
    app = create_web_app(config=owner.config, accounts=accounts, owner=owner)
    with TestClient(app) as client:

        def call(method, path, credential, body=None):
            return client.request(method, path, json=body, headers={"Authorization": "Bearer " + credential})

        assert call("POST", "/api/admin/accounts", admin, {"username": "second", "role": "admin"}).status_code == 409
        assert call("POST", "/api/admin/accounts", admin, {"username": "operator", "role": "viewer"}).status_code == 201
        assert call("POST", "/api/admin/password", viewer, {"secret": NEW}).status_code == 403, "a read-only user cannot"
        assert call("POST", "/api/admin/password", admin, {"secret": "short"}).status_code == 422
        assert call("POST", "/api/admin/password", admin, {"secret": NEW}).status_code == 200
        assert call("GET", "/api/me", admin).status_code == 401, "the old credential stops working at once"
        assert call("GET", "/api/me", "admin:" + NEW).json() == {"username": "admin", "role": "admin"}


def test_init_accounts_writes_the_only_administrator_into_the_file_with_its_record(archive_db_factory, tmp_path):
    """What the deployment runs.  Before 修复轮02 it wrote an administrator AND a viewer into the file only; since
    2026-10-30 the file is the only copy of the accounts and the database keeps the record."""
    store = archive_db_factory("primary")
    store.initialize(small_config())
    path = tmp_path / "accounts.json"
    saved = cli.init_accounts(path, store.dsn)
    assert saved == tmp_path / "initial-credentials.json"
    handed = json.loads(saved.read_text(encoding="utf-8"))
    credential = f"{handed['username']}:{handed['password']}"
    assert set(Accounts(path).users) == {"admin"}
    assert Accounts(path).verify(credential)["role"] == "admin", "the credential handed over works"
    assert [entry["action"] for entry in store.audit_history()["items"]] == ["CREATE_ACCOUNT"], "and is on record"
    handed_over = {p: p.read_bytes() for p in (saved, tmp_path / "admin-fallback.json")}
    assert cli.init_accounts(path, store.dsn) is None, "a second run makes no second administrator"
    path.unlink()
    assert cli.init_accounts(path, store.dsn) is None, "the fallback is there: a new administrator would void the password handed over"
    assert Accounts(path).verify(credential)["role"] == "admin", "the handed-over password still signs in, as the fallback"
    assert {p: p.read_bytes() for p in handed_over} == handed_over, "the initial password and its fallback stay as handed over"
