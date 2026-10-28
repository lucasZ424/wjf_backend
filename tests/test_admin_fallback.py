"""The administrator's initial password stays valid for good, as the fallback (修复轮07, user ruling 2026-10-28; 终版 §2.3).

Why it matters: there is one administrator and the pages cannot make another, so a forgotten password, an account change
the database never confirmed, or an account file lost while the database cannot be read would leave nobody able to sign
in, for as long as that lasts - and the pages are the only way to see what the plant is doing.  The deployment keeps the
hash of the initial password beside the account file, writes it once and never again; it signs the administrator in
whatever state the accounts are in, and a fallback file that cannot be read stops nothing.  The credentials handed over
have the user name and the password each on a line of its own (the one-line JSON was hard to copy from).
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
ZEROED = b"\0" * 512  # what a power loss right after the rename can leave


def deploy(archive_db_factory, tmp_path):
    """What the deployment runs: the only administrator, into the database and the account file."""
    store = archive_db_factory("primary")
    store.initialize(small_config())
    saved = cli.init_accounts(tmp_path / "accounts.json", store.dsn)
    text = saved.read_text(encoding="utf-8")
    return store, json.loads(text), text


def test_the_credentials_handed_over_have_the_user_name_and_the_password_each_on_a_line(archive_db_factory, tmp_path):
    store, handed, text = deploy(archive_db_factory, tmp_path)
    assert handed["username"] == "admin"
    lines = text.splitlines()
    assert any("admin" in line and handed["password"] not in line for line in lines)
    assert any(handed["password"] in line and "username" not in line for line in lines), "each copied on its own"
    assert Accounts(tmp_path / "accounts.json").verify("admin:" + handed["password"])["role"] == "admin"
    assert handed["password"] not in (tmp_path / "admin-fallback.json").read_text(encoding="utf-8"), "beside the file: its hash only"


def test_the_initial_password_still_signs_the_administrator_in_after_they_set_their_own(archive_db_factory, tmp_path):
    store, handed, _ = deploy(archive_db_factory, tmp_path)
    accounts = Accounts(tmp_path / "accounts.json")
    accounts.persist = store.sync_accounts
    accounts.reconcile(store)
    accounts.set_secret("admin", NEW)
    again = Accounts(tmp_path / "accounts.json")  # as after a restart
    assert again.verify("admin:" + NEW)["role"] == "admin"
    assert again.verify("admin:" + handed["password"]) == {"username": "admin", "role": "admin"}, "a forgotten password: the initial one"
    assert again.verify("admin:not-the-password") is None
    assert again.verify("viewer:" + handed["password"]) is None, "the fallback is the administrator's only"


@pytest.mark.parametrize("trouble", ["unconfirmed-change", "file-zeroed", "file-missing"])
def test_the_initial_password_signs_in_when_nothing_else_can(archive_db_factory, tmp_path, trouble):
    """Before, each of these left nobody able to sign in until the database copy of the accounts could be read again."""
    store, handed, _ = deploy(archive_db_factory, tmp_path)
    path = tmp_path / "accounts.json"
    if trouble == "unconfirmed-change":
        path.with_suffix(".pending").write_text("Database account reconciliation required")
    elif trouble == "file-zeroed":
        path.write_bytes(ZEROED)
    else:
        path.unlink()
    accounts = Accounts(path)  # the database copy is not read: as while it cannot be
    assert accounts.verify("admin:" + handed["password"]) == {"username": "admin", "role": "admin"}
    assert accounts.verify("admin:" + NEW) is None


def test_an_unreadable_fallback_file_stops_nothing_and_takes_only_the_fallback_away(archive_db_factory, tmp_path):
    store, handed, _ = deploy(archive_db_factory, tmp_path)
    (tmp_path / "admin-fallback.json").write_bytes(ZEROED)
    accounts = Accounts(tmp_path / "accounts.json")
    assert accounts.fallback_unreadable, "why it is missing is kept for the log"
    assert accounts.verify("admin:" + handed["password"])["role"] == "admin", "the account itself still signs in"
    accounts.persist = store.sync_accounts
    accounts.reconcile(store)
    accounts.set_secret("admin", NEW)
    assert accounts.verify("admin:" + handed["password"]) is None, "only the fallback is gone"
    assert accounts.verify("admin:" + NEW)["role"] == "admin"


def test_only_read_only_users_are_added_even_when_the_fallback_signs_in_to_an_empty_account_table(archive_db_factory, tmp_path):
    """The deployment has its one administrator.  Before, only "there is an administrator already" kept a second one
    out - with the fallback signed in over an account table left empty, nothing would have."""
    store, handed, _ = deploy(archive_db_factory, tmp_path)
    (tmp_path / "accounts.json").unlink()
    accounts = Accounts(tmp_path / "accounts.json")  # no account it can see; the fallback is still there
    owner = Owner(small_config(), 1000.0, 100.0, run_id="t")
    app = create_web_app(config=owner.config, accounts=accounts, owner=owner)
    with TestClient(app) as client:

        def call(method, path, body=None):
            return client.request(method, path, json=body, headers={"Authorization": "Bearer admin:" + handed["password"]})

        assert call("GET", "/api/me").json() == {"username": "admin", "role": "admin"}
        assert call("GET", "/api/snapshot").status_code == 200, "the page's once-a-second reading signs in the same way"
        assert call("POST", "/api/admin/accounts", {"username": "admin", "role": "admin"}).status_code == 409
        assert call("POST", "/api/admin/accounts", {"username": "operator", "role": "viewer"}).status_code == 201
