import copy
import os
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from owner_support import small_config
from test_archive_router import Disks

from htsg.archive_router import ArchiveRouter
from htsg.auth import Accounts
from htsg.owner import Owner
from htsg.runtime import Runtime
from htsg.web import create_web_app


def test_no_reading_is_given_once_the_account_state_becomes_uncertain(tmp_path):
    """The live pages authenticate every reading (修复轮05): once the account state is unsettled, the very next reading
    is refused - there is no open channel left that would have to be cut off."""
    config = small_config()
    accounts = Accounts(tmp_path / "accounts.json")
    headers = {"Authorization": "Bearer " + accounts.create("admin", "admin")}
    app = create_web_app(config=config, accounts=accounts, owner=Owner(config, 1000.0, 100.0, run_id="t"))
    with TestClient(app) as client:
        assert client.get("/api/snapshot", headers=headers).status_code == 200
        accounts.uncertain = True
        assert client.get("/api/snapshot", headers=headers).status_code == 401


class AccountStore:
    def __init__(self):
        self.users = {}
        self.audit = []

    def read_accounts(self):
        return copy.deepcopy(self.users)

    def sync_accounts(self, users, audit=None):
        self.users = copy.deepcopy(users)
        if audit:
            self.audit.append(audit)


@pytest.fixture
def account_setup(tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    admin = accounts.create("admin", "admin")
    viewer = accounts.create("viewer", "viewer")
    store = AccountStore()
    accounts.persist = store.sync_accounts
    accounts.reconcile(store)
    return accounts, store, admin, viewer


def test_committed_revocation_cannot_be_undone_by_failed_local_cache(account_setup, monkeypatch):
    accounts, store, admin, viewer = account_setup
    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", lambda *args: (_ for _ in ()).throw(OSError("cache unavailable")))
        with pytest.raises(OSError):
            accounts.revoke("viewer", {"result": "COMMITTED"})
    assert not store.users["viewer"]["active"]
    assert store.audit == [{"result": "COMMITTED"}]
    assert accounts.verify(viewer) is None and accounts.verify(admin) is None
    restarted = Accounts(accounts.path)
    assert restarted.uncertain and restarted.verify(viewer) is None
    restarted.persist = store.sync_accounts
    restarted.reconcile(store)
    assert restarted.verify(viewer) is None
    assert restarted.verify(admin)["role"] == "admin"
    assert not restarted.pending_path.exists()


@pytest.mark.parametrize("committed", [False, True])
def test_unknown_database_result_requires_reconciliation_before_authentication(account_setup, committed):
    accounts, store, admin, viewer = account_setup

    def uncertain_write(candidate, audit):
        if committed:
            store.sync_accounts(candidate, audit)
        raise psycopg.OperationalError("commit reply unavailable")

    accounts.persist = uncertain_write
    with pytest.raises(psycopg.OperationalError):
        accounts.revoke("viewer", {"result": "COMMITTED"})
    assert accounts.verify(viewer) is None
    with pytest.raises(RuntimeError, match="reconciliation"):
        accounts.create("other", "viewer")
    accounts.reconcile(store)
    assert (accounts.verify(viewer) is None) == committed
    assert accounts.verify(admin)["role"] == "admin"
    assert len(store.audit) == int(committed)


def test_pending_change_over_an_empty_database_bootstraps_it_from_the_local_copy(account_setup):
    """A first start stopped between the marker and the database (a power loss, the database gone) used to refuse every
    later start with RuntimeError.  The database holds nothing, so the local copy is all there is (修复轮02, user ruling
    2026-10-26): it bootstraps the database, and a change that never reached the database stays undone."""
    accounts, _, admin, _ = account_setup
    accounts.begin_update()
    empty = AccountStore()
    accounts.persist = empty.sync_accounts
    accounts.reconcile(empty)
    assert not accounts.uncertain and not accounts.pending_path.exists()
    assert empty.users == accounts.users and accounts.verify(admin)["role"] == "admin"


@pytest.mark.parametrize("contents", [b"", b'{"admin": {"role": "adm', b"\0" * 512, b"\xff\xfe\x00\x81"], ids=["empty", "torn", "zeroed", "garbled"])
def test_a_damaged_account_file_counts_as_no_local_copy_and_is_rewritten_from_the_database(tmp_path, contents):
    """What a power loss can leave (review item 3).  The file only caches the database copy: a damaged one lets nobody in
    from it, says why, and reading the database copy writes it whole again.  Before, the constructor raised, so the
    backend could not start."""
    earlier = Accounts(tmp_path / "earlier.json")
    admin = earlier.create("admin", "admin")
    store = AccountStore()
    store.users = copy.deepcopy(earlier.users)
    path = tmp_path / "accounts.json"
    path.write_bytes(contents)
    accounts = Accounts(path)
    assert accounts.users == {} and accounts.unreadable and accounts.verify(admin) is None
    accounts.persist = store.sync_accounts
    accounts.reconcile(store)
    assert accounts.verify(admin)["role"] == "admin" and Accounts(path).users == store.users


def test_the_account_file_is_on_the_disk_before_its_name_points_to_it(tmp_path, monkeypatch):
    """Without a flush to the disk a power loss right after the rename can leave the name over contents that never got
    there - an empty or zeroed file (review item 3).  So: the marker is on the disk before the database is asked, and the
    new file before it replaces the old one."""
    events = []
    real_fsync, real_replace = os.fsync, Path.replace
    monkeypatch.setattr(os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(Path, "replace", lambda self, target: (events.append("replace " + self.suffix), real_replace(self, target))[1])
    accounts = Accounts(tmp_path / "accounts.json")
    accounts.persist = lambda users, audit=None: events.append("database")
    accounts.create("admin", "admin", {"result": "COMMITTED"})
    assert events == ["fsync", "database", "fsync", "replace .tmp"]
    assert Accounts(accounts.path).users.keys() == {"admin"}


def test_database_snapshot_wins_over_clean_stale_cache(account_setup):
    accounts, store, admin, viewer = account_setup
    store.users["viewer"]["active"] = False
    accounts.reconcile(store)
    assert accounts.verify(viewer) is None and accounts.verify(admin)
    assert not Accounts(accounts.path).users["viewer"]["active"]


@pytest.mark.parametrize("pending", [False, True])
async def test_offline_cache_allowed_only_without_unresolved_change(archive_db_factory, tmp_path, monkeypatch, pending):
    """The runtime settles the accounts with the database when it starts (終版 §2.3): a clean local cache may carry on
    while the database cannot be read, an unfinished change may not - nobody signs in, yet the backend starts (account
    trouble never stops the run, 修复轮02; it used not to start at all) and settles later (test_account_startup)."""
    config = small_config(sources=[{"id": "mb", "kind": "modbus"}, {"id": "ua", "kind": "opcua"}])
    router = ArchiveRouter(config, archive_db_factory("primary"), archive_db_factory("emergency"), disk_usage=Disks())
    accounts = Accounts(tmp_path / "accounts.json")
    admin = accounts.create("admin", "admin")
    if pending:
        accounts.begin_update()

    def unavailable():
        raise psycopg.OperationalError("database read unavailable")

    monkeypatch.setattr(router.primary, "read_accounts", unavailable)
    runtime = Runtime(config, router, accounts, number_file=tmp_path / "bucket_number.json")
    await runtime.start()
    try:
        assert runtime.running
        if pending:
            assert runtime.health["accounts"] == "RECONCILIATION_REQUIRED" and accounts.verify(admin) is None
        else:
            assert runtime.health["accounts"] == "LOCAL_VERIFICATION_CACHE_ONLY" and accounts.verify(admin)
    finally:
        await runtime.stop()


def test_invalid_audit_rolls_back_account_transaction(archive_db_factory, tmp_path):
    accounts = Accounts(tmp_path / "accounts.json")
    accounts.create("admin", "admin")
    accounts.create("viewer", "viewer")
    store = archive_db_factory("primary")
    store.initialize(small_config())
    store.sync_accounts(accounts.users)
    candidate = copy.deepcopy(accounts.users)
    candidate["viewer"]["active"] = False
    with pytest.raises(psycopg.Error):
        store.sync_accounts(
            candidate,
            {
                "id": "invalid",
                "time": 1,
                "actor": None,
                "action": "REVOKE_ACCOUNT",
                "target": "viewer",
                "result": "COMMITTED",
            },
        )
    assert store.read_accounts()["viewer"]["active"]
    assert store.audit_history()["items"] == []
