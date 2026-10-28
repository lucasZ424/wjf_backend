"""Account trouble never stops the backend (review item 3, 修复轮02, user ruling 2026-10-26): the start and the acquisition
go on, nobody signs in until the database copy of the accounts has been read, and the backend tries again by itself -
no restart is needed.

Why it matters (the hard rule, a year without a crash): the account file is only a cache of the database's copy and only
decides who may sign in, yet a damaged or missing file used to stop the whole backend - acquisition and archive with it -
and under the supervisor that became a start failing for ever.
"""

import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError

import psycopg
import pytest
from owner_support import small_config
from test_archive_router import Disks

from htsg.archive_router import ArchiveRouter
from htsg.auth import Accounts
from htsg.runtime import Runtime
from htsg.simulation import FieldSimulator
from htsg.web import audit_entry

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import measure  # noqa: E402
import verify_resilience as resilience  # noqa: E402

ZEROED = b"\0" * 512  # what a power loss right after the rename can leave: the name over contents never written to the disk


async def settled(condition, why, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, why
        await asyncio.sleep(0.1)


def quick_retry_config():
    return small_config(timing={"status_interval": 0.1, "stats_interval": 0.2})  # maintenance retries within a test's time


class Trouble:
    """Makes what it wraps fail until it is cleared."""

    def __init__(self, error):
        self.error, self.on = error, True

    def wrap(self, real):
        def call(*args, **kwargs):
            if self.on:
                raise self.error
            return real(*args, **kwargs)

        return call


# ------------------------------------------------------------------------------------- in-process, real databases


async def test_a_change_the_database_did_not_confirm_locks_sign_in_only_until_the_database_is_read_again(archive_db_factory, tmp_path, monkeypatch):
    """Before, a revocation whose database write failed left everybody - the administrator too - unable to sign in until
    the backend was restarted, and nothing restarted it: the process was still running."""
    config = quick_retry_config()
    router = ArchiveRouter(config, archive_db_factory("primary"), archive_db_factory("emergency"), disk_usage=Disks())
    accounts = Accounts(tmp_path / "accounts.json")
    admin, viewer = accounts.create("admin", "admin"), accounts.create("viewer", "viewer")
    runtime = Runtime(config, router, accounts)
    await runtime.start()
    try:
        assert runtime.health["accounts"] == "DATABASE_AUTHORITATIVE"
        away = Trouble(psycopg.OperationalError("database gone"))
        with monkeypatch.context() as patch:
            patch.setattr(accounts, "persist", away.wrap(accounts.persist))  # the change cannot be confirmed ...
            patch.setattr(router.primary, "read_accounts", away.wrap(router.primary.read_accounts))  # ... nor read back yet
            with pytest.raises(psycopg.OperationalError):
                await asyncio.to_thread(accounts.revoke, "viewer", audit_entry({"username": "admin"}, "REVOKE_ACCOUNT", "viewer"))
            await asyncio.sleep(0.6)  # a few maintenance rounds while the database stays away
            assert accounts.verify(admin) is None, "nobody signs in on a change whose outcome is unknown"
        await settled(lambda: accounts.verify(admin) is not None, "sign-in comes back once the database is read, without a restart")
        assert accounts.verify(viewer), "the revocation never reached the database, and the database copy is what counts"
        assert runtime.health["accounts"] == "DATABASE_AUTHORITATIVE" and not accounts.pending_path.exists()
        assert not runtime.failed.is_set()
    finally:
        await runtime.stop()


@pytest.mark.parametrize("state, trouble", [("pending", "database"), ("damaged", "database"), ("clean", "file")])
async def test_account_trouble_at_the_start_never_stops_it_and_settles_once_it_can(archive_db_factory, tmp_path, monkeypatch, state, trouble):
    """pending: a change the previous run left unfinished; damaged: the file a power loss left; clean: a good file.
    database: the account table cannot be read; file: the account file cannot be written.  Before, each of these
    ended the start (the first with the database error, the second already in the constructor, the third with OSError)."""
    config = quick_retry_config()
    primary = archive_db_factory("primary")
    router = ArchiveRouter(config, primary, archive_db_factory("emergency"), disk_usage=Disks())
    previous = Accounts(tmp_path / "previous.json")  # what an earlier run left in the database
    admin = previous.create("admin", "admin")
    primary.initialize(config)
    primary.sync_accounts(previous.users)
    path = tmp_path / "accounts.json"
    if state == "damaged":
        path.write_bytes(ZEROED)
    else:
        path.write_text(json.dumps(previous.users), encoding="utf-8")
    accounts = Accounts(path)
    if state == "pending":
        accounts.begin_update()
    if trouble == "database":
        away = Trouble(psycopg.OperationalError("account table unreadable"))
        monkeypatch.setattr(primary, "read_accounts", away.wrap(primary.read_accounts))
    else:
        away = Trouble(OSError("account file cannot be written"))
        monkeypatch.setattr(Path, "replace", away.wrap(Path.replace))
    runtime = Runtime(config, router, accounts)
    await runtime.start()
    try:
        assert runtime.running and not runtime.failed.is_set(), "the start went on"
        assert accounts.verify(admin) is None, "nobody signs in before the database copy has been read into the file"
        away.on = False
        await settled(lambda: accounts.verify(admin) is not None, "sign-in comes back without a restart")
        assert runtime.health["accounts"] == "DATABASE_AUTHORITATIVE" and not accounts.pending_path.exists()
        assert Accounts(path).users == primary.read_accounts(), "the file is the database copy again"
    finally:
        await runtime.stop()


# --------------------------------------------------------- the delivered entry point, as a process of its own


def launch(work, config, primary, emergency):
    (work / "config.json").write_text(json.dumps(config.model_dump(mode="json")), encoding="utf-8")
    (work / "database.json").write_text(json.dumps({"primary": primary.dsn, "emergency": emergency.dsn}), encoding="utf-8")
    backend = resilience.Backend(work, work / "config.json", work / "database.json", work / "accounts.json")
    backend.start()
    return backend


def output(work):
    return " | ".join(p.read_text(errors="replace")[-1500:] for p in work.glob("backend-*.log"))


async def answer(backend, path, credential="", timeout=60):
    """What the backend answers once it listens; it must not have ended meanwhile."""
    deadline = time.monotonic() + timeout
    while True:
        assert backend.proc.poll() is None, f"the backend ended with {backend.proc.returncode}: {output(backend.work)}"
        try:
            return await asyncio.to_thread(resilience.fetch, f"http://127.0.0.1:{backend.port}{path}", credential)
        except HTTPError:
            raise
        except OSError:  # not listening yet
            assert time.monotonic() < deadline, f"no answer from {path} within {timeout} s"
            await asyncio.sleep(0.3)


def groups(store) -> int:
    try:
        with psycopg.connect(store.dsn) as conn:
            return conn.execute("SELECT count(*) FROM commit_group").fetchone()[0]
    except psycopg.errors.UndefinedTable:  # the backend has not laid down the schema yet
        return 0


@pytest.mark.parametrize("damage", ["zeroed", "missing"])
async def test_a_damaged_or_missing_account_file_does_not_keep_the_backend_from_starting(archive_db_factory, tmp_path, damage):
    """`python -m htsg.cli run`: before, a zeroed file stopped it with JSONDecodeError and a missing one with
    "Run init-accounts first", although the database held the accounts."""
    config = measure.make_config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    accounts = Accounts(tmp_path / "accounts.json")
    viewer = accounts.create("viewer", "viewer")
    accounts.create("admin", "admin")
    primary.initialize(config)
    primary.sync_accounts(accounts.users)  # what an earlier run left in the database
    if damage == "zeroed":
        accounts.path.write_bytes(ZEROED)
    else:
        accounts.path.unlink()
    backend = launch(tmp_path, config, primary, emergency)
    try:
        assert (await answer(backend, "/api/me", viewer))["role"] == "viewer", "the database copy lets the viewer in"
        assert Accounts(accounts.path).users == primary.read_accounts(), "the file is rewritten from the database copy"
        if damage == "zeroed":
            assert "account file unreadable" in (tmp_path / ".local/backend.log").read_text(encoding="utf-8"), "and the log says so"
        assert isinstance(await backend.stop(), int), "it stops the normal way"
    finally:
        backend.ensure_gone()


async def test_an_unreadable_fallback_file_does_not_keep_the_backend_from_starting(archive_db_factory, tmp_path):
    """修复轮07 (终版 §2.3): the file beside the account file that keeps the administrator's initial password is read at
    the start; one that cannot be read takes only the fallback away, and the log says so."""
    config = measure.make_config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    accounts = Accounts(tmp_path / "accounts.json")
    viewer = accounts.create("viewer", "viewer")
    accounts.create("admin", "admin")
    primary.initialize(config)
    primary.sync_accounts(accounts.users)
    (tmp_path / "admin-fallback.json").write_bytes(ZEROED)
    backend = launch(tmp_path, config, primary, emergency)
    try:
        assert (await answer(backend, "/api/me", viewer))["role"] == "viewer", "the accounts sign in as before"
        assert "fallback credential unreadable" in (tmp_path / ".local/backend.log").read_text(encoding="utf-8"), "and the log says so"
        assert isinstance(await backend.stop(), int), "it stops the normal way"
    finally:
        backend.ensure_gone()


async def test_with_no_account_anywhere_the_backend_still_acquires_and_archives_and_says_what_to_do(archive_db_factory, tmp_path):
    """User ruling 2026-10-26: no account in the database nor in the file is no reason to stop acquiring.  Nobody can sign
    in, and the log says to run init-accounts.  Before, the start ended at once with "Run init-accounts first"."""
    config = measure.make_config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    field = FieldSimulator(config)
    await field.start()
    backend = launch(tmp_path, config, primary, emergency)
    try:
        assert (await answer(backend, "/live"))["alive"]
        await settled(lambda: groups(primary) > 0, "what is acquired reaches the archive", timeout=30)
        with pytest.raises(HTTPError) as refused:
            await asyncio.to_thread(resilience.fetch, f"http://127.0.0.1:{backend.port}/api/me", "admin:admin")
        assert refused.value.code == 401, "nobody can sign in, the development shortcut included"
        assert "init-accounts" in (tmp_path / ".local/backend.log").read_text(encoding="utf-8")
        assert isinstance(await backend.stop(), int), "it stops the normal way"
    finally:
        backend.ensure_gone()
        await field.stop()
