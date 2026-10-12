import copy
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from owner_support import small_config
from test_archive_router import Disks

from htsg.archive_router import ArchiveRouter
from htsg.auth import Accounts
from htsg.owner import Owner
from htsg.publisher import Publisher
from htsg.runtime import Runtime
from htsg.web import create_web_app


def test_waiting_websocket_does_not_deliver_data_after_account_state_becomes_uncertain(tmp_path):
    from starlette.websockets import WebSocketDisconnect

    config = small_config()
    accounts = Accounts(tmp_path / "accounts.json")
    credential = accounts.create("admin", "admin")
    app = create_web_app(config=config, accounts=accounts, publisher=Publisher(Owner(config, 1000.0, 100.0, run_id="t")))
    with TestClient(app) as client:
        with client.websocket_connect("/api/stream") as ws:
            ws.send_json({"credential": credential})
            assert ws.receive_json()["type"] == "snapshot"
            accounts.uncertain = True
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()  # the heartbeat finds the account uncertain and nothing is sent
            assert closed.value.code == 1008


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


def test_pending_change_cannot_bootstrap_database_from_stale_cache(account_setup):
    accounts, _, _, _ = account_setup
    accounts.begin_update()
    with pytest.raises(RuntimeError, match="empty database"):
        accounts.reconcile(AccountStore())
    assert accounts.uncertain


def test_database_snapshot_wins_over_clean_stale_cache(account_setup):
    accounts, store, admin, viewer = account_setup
    store.users["viewer"]["active"] = False
    accounts.reconcile(store)
    assert accounts.verify(viewer) is None and accounts.verify(admin)
    assert not Accounts(accounts.path).users["viewer"]["active"]


@pytest.mark.parametrize("pending", [False, True])
async def test_offline_cache_allowed_only_without_unresolved_change(archive_db_factory, tmp_path, monkeypatch, pending):
    """The runtime settles the accounts with the database when it starts (終版 §2.3): a clean local cache may carry on
    while the database cannot be read, an unfinished change may not - then the backend does not start at all."""
    config = small_config(sources=[{"id": "mb", "kind": "modbus"}, {"id": "ua", "kind": "opcua"}])
    router = ArchiveRouter(config, archive_db_factory("primary"), archive_db_factory("emergency"), disk_usage=Disks())
    accounts = Accounts(tmp_path / "accounts.json")
    accounts.create("admin", "admin")
    if pending:
        accounts.begin_update()

    def unavailable():
        raise psycopg.OperationalError("database read unavailable")

    monkeypatch.setattr(router.primary, "read_accounts", unavailable)
    runtime = Runtime(config, router, accounts)
    if pending:
        with pytest.raises(psycopg.OperationalError):
            await runtime.start()
        assert not runtime.running and runtime.health["accounts"] == "RECONCILIATION_REQUIRED"
    else:
        await runtime.start()
        try:
            assert runtime.health["accounts"] == "LOCAL_VERIFICATION_CACHE_ONLY"
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
