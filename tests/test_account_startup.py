"""Account trouble never stops the backend (review item 3, 修复轮02, user ruling 2026-10-26): the start and the acquisition
go on whatever the account file looks like; since 2026-10-30 that file is the only copy, and when it is lost the
administrator's initial password is what signs in.

Why it matters (the hard rule, a year without a crash): the account file only decides who may sign in, yet a damaged or
missing file used to stop the whole backend - acquisition and archive with it - and under the supervisor that became a
start failing for ever.
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
from htsg.auth import Accounts, write_fallback
from htsg.runtime import Runtime
from htsg.simulation import FieldSimulator

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import measure  # noqa: E402
import verify_resilience as resilience  # noqa: E402

ZEROED = b"\0" * 512  # what a power loss right after the rename can leave: the name over contents never written to the disk


async def settled(condition, why, timeout=10.0):
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, why
        await asyncio.sleep(0.1)


# ------------------------------------------------------------------------------------- in-process, real databases


@pytest.mark.parametrize("state, health", [("clean", "FILE"), ("damaged", "FILE_UNREADABLE"), ("missing", "NO_ACCOUNTS")])
async def test_account_trouble_at_the_start_never_stops_it_and_the_health_says_which(archive_db_factory, tmp_path, state, health):
    """The account file is the only copy (2026-10-30): a damaged or missing one stops nothing, lets only the initial
    password in, and the self-check says so.  Before, the start waited on the database copy to settle it."""
    config = small_config()
    router = ArchiveRouter(config, archive_db_factory("primary"), archive_db_factory("emergency"), disk_usage=Disks())
    path = tmp_path / "accounts.json"
    admin = Accounts(path).create("admin", "admin")
    write_fallback(path, "initial-password")
    if state == "damaged":
        path.write_bytes(ZEROED)
    elif state == "missing":
        path.unlink()
    accounts = Accounts(path)
    runtime = Runtime(config, router, accounts, number_file=tmp_path / "bucket_number.json")
    await runtime.start()
    try:
        assert runtime.running and not runtime.failed.is_set(), "the start went on"
        assert runtime.health["accounts"] == health
        assert (accounts.verify(admin) is not None) == (state == "clean")
        assert accounts.verify("admin:initial-password") == {"username": "admin", "role": "admin"}
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
    "Run init-accounts first".  The file is the only copy now (2026-10-30): what is left is the initial password."""
    config = measure.make_config()
    primary, emergency = archive_db_factory("primary"), archive_db_factory("emergency")
    accounts = Accounts(tmp_path / "accounts.json")
    accounts.create("viewer", "viewer")
    accounts.create("admin", "admin")
    write_fallback(accounts.path, "initial-password")
    primary.initialize(config)
    if damage == "zeroed":
        accounts.path.write_bytes(ZEROED)
    else:
        accounts.path.unlink()
    backend = launch(tmp_path, config, primary, emergency)
    try:
        assert (await answer(backend, "/api/me", "admin:initial-password"))["role"] == "admin", "the initial password signs in"
        said = "account file unreadable" if damage == "zeroed" else "init-accounts"
        assert said in (tmp_path / ".local/backend.log").read_text(encoding="utf-8"), "and the log says so"
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
