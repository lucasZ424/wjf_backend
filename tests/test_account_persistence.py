"""The account file is the accounts (2026-10-30 audit, user ruling): one copy, no reconciliation with the database.

Why it matters: the database copy and its reconciliation made a state in which nobody but the fallback could sign in -
on a single-host monitor whose pages are the only way to see the plant.  What is kept: every change is on record (a change
whose management record cannot be written is refused, user ruling 2026-10-30), a power loss leaves the old file or the new
one, and every request is authenticated - a credential that passed scrypt once is recognised from memory until the next
change, so a page reading every second no longer costs a deliberately slow hash each time.
"""

import os
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from owner_support import small_config
from test_archive_router import Disks

from htsg import auth
from htsg.archive_router import ArchiveRouter
from htsg.auth import Accounts, write_fallback
from htsg.owner import Owner
from htsg.runtime import Runtime
from htsg.web import create_web_app

NEW = "a-password-of-my-own"


class Record:
    """The archive's management log, or its failure."""

    def __init__(self):
        self.entries, self.error = [], None

    def __call__(self, entry):
        if self.error is not None:
            raise self.error
        self.entries.append(entry)


@pytest.fixture
def account_setup(tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    admin = accounts.create("admin", "admin")
    viewer = accounts.create("viewer", "viewer")
    record = Record()
    accounts.record = record
    return accounts, record, admin, viewer


def test_a_change_takes_effect_with_its_record_and_survives_a_restart(account_setup):
    accounts, record, admin, viewer = account_setup
    accounts.revoke("viewer", {"action": "REVOKE_ACCOUNT"})
    assert record.entries == [{"action": "REVOKE_ACCOUNT"}]
    assert accounts.verify(viewer) is None and Accounts(accounts.path).verify(viewer) is None
    assert Accounts(accounts.path).verify(admin)["role"] == "admin"


@pytest.mark.parametrize("change", ["create", "revoke", "set_secret"])
def test_a_change_whose_record_cannot_be_written_is_not_made(account_setup, change):
    """User ruling 2026-10-30: no record, no change.  Before, the database decided and an unconfirmed change locked out
    everybody but the fallback until the database could be read again."""
    accounts, record, admin, viewer = account_setup
    record.error = psycopg.OperationalError("database down")
    before = Path(accounts.path).read_bytes()
    with pytest.raises(psycopg.OperationalError):
        {
            "create": lambda: accounts.create("other", "viewer", {"action": "CREATE_ACCOUNT"}),
            "revoke": lambda: accounts.revoke("viewer", {"action": "REVOKE_ACCOUNT"}),
            "set_secret": lambda: accounts.set_secret("admin", NEW, {"action": "SET_PASSWORD"}),
        }[change]()
    assert Path(accounts.path).read_bytes() == before and not accounts.path.with_suffix(".tmp").exists()
    assert accounts.verify(admin) and accounts.verify(viewer), "everybody signs in as before: nothing is locked"
    assert "other" not in accounts.users and accounts.verify("admin:" + NEW) is None


def test_the_new_file_is_on_the_disk_before_the_record_and_takes_its_place_only_after_it(tmp_path, monkeypatch):
    """Without a flush a power loss right after the rename can leave the name over contents that never got there; without
    the record first a change could be made that is on no record."""
    events = []
    real_fsync, real_replace = os.fsync, Path.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(Path, "replace", lambda self, target: (events.append("replace " + self.suffix), real_replace(self, target))[1])
    accounts = Accounts(tmp_path / "accounts.json")
    accounts.record = lambda entry: events.append("record")
    accounts.create("admin", "admin", {"result": "COMMITTED"})
    assert events == ["fsync", "record", "replace .tmp"]
    assert Accounts(accounts.path).users.keys() == {"admin"}


@pytest.mark.parametrize("contents", [b"", b'{"admin": {"role": "adm', b"\0" * 512, b"\xff\xfe\x00\x81", b"[]"], ids=["empty", "torn", "zeroed", "garbled", "not-a-table"])
def test_a_damaged_account_file_lets_only_the_fallback_in_and_its_password_change_brings_the_administrator_back(tmp_path, contents):
    """What a power loss or a disk fault can leave.  There is no other copy now: the initial password signs in, and the
    administrator's password change on the page makes the account again; the read-only users are added again."""
    path = tmp_path / "accounts.json"
    write_fallback(path, "initial-password")
    path.write_bytes(contents)
    accounts = Accounts(path)
    assert accounts.users == {} and accounts.unreadable
    assert accounts.verify("admin:initial-password") == {"username": "admin", "role": "admin"}
    accounts.set_secret("admin", NEW)
    assert Accounts(path).verify("admin:" + NEW) == {"username": "admin", "role": "admin"}
    with pytest.raises(ValueError, match="没有这个账户"):
        accounts.set_secret("viewer", NEW)


def test_a_verified_credential_costs_no_second_scrypt_until_the_next_change(account_setup, monkeypatch):
    accounts, _, admin, viewer = account_setup
    calls = []
    real = auth._digest
    monkeypatch.setattr(auth, "_digest", lambda secret, salt: (calls.append(1), real(secret, salt))[1])
    for _ in range(5):
        assert accounts.verify(viewer)["role"] == "viewer"
    assert len(calls) == 1, "a page reading every second hashes once, not every second"
    assert accounts.verify("viewer:wrong") is None and accounts.verify("viewer:wrong") is None
    assert len(calls) == 3, "a wrong credential is never remembered: each try costs the full hash"
    accounts.revoke("viewer")
    assert accounts.verify(viewer) is None, "a revocation ends the remembered credential at once"
    accounts.set_secret("admin", NEW)
    assert accounts.verify(admin) is None, "the old password stops working at once"


def test_a_check_that_began_before_a_change_is_not_remembered(account_setup, monkeypatch):
    """The slow hash runs outside the lock: a revocation can land while a check of the same credential is under way.  What
    that check found was true before the change only, so it must not be kept for after it."""
    accounts, _, _, viewer = account_setup
    real = accounts._check

    def check_then_revoke(credential):
        found = real(credential)
        accounts.revoke("viewer")
        return found

    monkeypatch.setattr(accounts, "_check", check_then_revoke)
    assert accounts.verify(viewer)["role"] == "viewer", "this one answer came before the change"
    monkeypatch.setattr(accounts, "_check", real)
    assert accounts.verify(viewer) is None


def test_the_memory_of_verified_credentials_never_grows_beyond_its_limit(tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    credentials = [accounts.create(f"v{i}", "viewer") for i in range(auth.VERIFIED_LIMIT + 5)]
    for credential in credentials:
        assert accounts.verify(credential)
    assert 0 < len(accounts._verified) <= auth.VERIFIED_LIMIT


async def test_through_the_backend_a_change_is_on_record_in_the_archive_and_refused_when_it_cannot_be(archive_db_factory, tmp_path, monkeypatch):
    config = small_config()
    router = ArchiveRouter(config, archive_db_factory("primary"), archive_db_factory("emergency"), disk_usage=Disks())
    accounts = Accounts(tmp_path / "accounts.json")
    admin = {"Authorization": "Bearer " + accounts.create("admin", "admin")}
    runtime = Runtime(config, router, accounts, number_file=tmp_path / "bucket_number.json")
    app = create_web_app(config=config, accounts=accounts, owner=runtime.owner, archive_router=router, lifecycle=runtime)
    with TestClient(app, raise_server_exceptions=False) as client:
        assert runtime.health["accounts"] == "FILE"
        assert client.post("/api/admin/accounts", json={"username": "op", "role": "viewer"}, headers=admin).status_code == 201
        assert [e["action"] for e in client.get("/api/admin/audit", headers=admin).json()["items"]] == ["CREATE_ACCOUNT"]

        def down(entry):
            raise psycopg.OperationalError("database down")

        monkeypatch.setattr(accounts, "record", down)
        answer = client.post("/api/admin/accounts", json={"username": "op2", "role": "viewer"}, headers=admin)
        assert answer.status_code == 503 and "变更未确认" in answer.json()["detail"]
        assert "op2" not in Accounts(accounts.path).users
        assert client.get("/api/snapshot", headers=admin).status_code == 200, "and nobody is locked out by it"


def test_every_reading_stops_being_let_in_the_moment_the_account_is_revoked(tmp_path):
    """The live pages authenticate every reading (修复轮05): a revocation refuses the very next one, remembered or not."""
    config = small_config()
    accounts = Accounts(tmp_path / "accounts.json")
    accounts.create("admin", "admin")
    headers = {"Authorization": "Bearer " + accounts.create("viewer", "viewer")}
    app = create_web_app(config=config, accounts=accounts, owner=Owner(config, 1000.0, 100.0, run_id="t"))
    with TestClient(app) as client:
        assert client.get("/api/snapshot", headers=headers).status_code == 200
        accounts.revoke("viewer")
        assert client.get("/api/snapshot", headers=headers).status_code == 401
