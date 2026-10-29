import argparse
import asyncio
import json
import logging
import secrets
from contextlib import contextmanager, nullcontext
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .auth import Accounts, write_fallback
from .config import load_settings


@contextmanager
def deployment_lock(path=Path(".local/backend.lock")):
    import msvcrt

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        try:
            lock.seek(0)
            if not lock.read(1):
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise SystemExit("Another backend already owns this deployment directory") from None
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def init_accounts(accounts: Path, primary_dsn: str, timing=None) -> Path | None:
    """The deployment's only administrator, written into the primary database and the account file together, so that a
    deployment never reaches its first start without an account (修复轮02).  Returns where its credential was saved, or None
    when the database or the file holds the administrator already.  Read-only users are added on the page.  The initial
    password is the fallback for good (修复轮07): its hash beside the account file, the password for the owner with the user
    name and the password each on a line."""
    from .archive import ArchiveStore
    from .web import audit_entry

    local, store = Accounts(accounts), ArchiveStore(primary_dsn, timing)
    try:
        local.persist = store.sync_accounts
        local.reconcile(store)  # the database copy first: it may hold the administrator already
        if any(user["role"] == "admin" for user in local.users.values()):
            return None
        password, destination = secrets.token_urlsafe(32), accounts.with_name("initial-credentials.json")
        # on the disk before the account is: a run cut short in between makes no administrator, and is run again
        write_fallback(accounts, password)
        destination.write_text(json.dumps({"username": "admin", "password": password}, indent=2) + "\n", encoding="utf-8")
        local.create("admin", "admin", audit_entry({"username": "init-accounts"}, "CREATE_ACCOUNT", "admin"), token=password)
    finally:
        store.close()
    return destination


BYTES_PER_QUANTITY_PER_DAY = 69 * 10**6  # measured at full scale, every value changing (终版 §13.4)


def capacity(config, disk_bytes=None, stop=None) -> dict:
    """How often the rolling deletion runs and how much is kept after it, for this configuration (终版 §13.4, §13.5).  A
    scenario from the measured daily growth, never an acceptance: records differ in width and rate, WAL, other files and the
    tables kept for good take room too, and deletion goes by whole buckets.  The self-check page shows the real pace."""
    import shutil

    policy = config.storage
    stop = policy.cleanup_stop if stop is None else stop
    disk_bytes = shutil.disk_usage(policy.primary_path).total if disk_bytes is None else disk_bytes
    quantities = len(config.points) + len(config.derived)
    per_day = quantities * BYTES_PER_QUANTITY_PER_DAY
    return {
        "archived_quantities": quantities,
        "bytes_per_quantity_per_day": BYTES_PER_QUANTITY_PER_DAY,
        "disk_bytes": disk_bytes,
        "cleanup_start": policy.cleanup_start,
        "cleanup_stop": stop,
        "days_between_deletions": (policy.cleanup_start - stop) * disk_bytes / per_day if per_day else None,
        "days_kept_after_deletion": stop * disk_bytes / per_day if per_day else None,
        "note": "Scenario only: the measured daily growth times the channels; adjust the stop line by the deletion period "
        "the self-check page measures",
    }


# the highest bucket number used, and the run time of the bucket in use (§13.6): the deployment's own, named here only
NUMBER_FILE = Path(".local/bucket_number.json")


def main(*, deployment_locked=False):
    parser = argparse.ArgumentParser(description="HTSG local monitoring backend")
    parser.add_argument("command", choices=["run", "check", "init-accounts", "capacity"])
    parser.add_argument("--config", default="config/development.json")
    parser.add_argument("--database", default=".local/database.json")
    parser.add_argument("--accounts", default=".local/accounts.json")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--disk-bytes", type=int, help="capacity: the size of the normal database's disk (default: as measured)")
    parser.add_argument("--cleanup-stop", type=float, help="capacity: the stop line to estimate for (default: the configured one)")
    args = parser.parse_args()
    config = load_settings(args.config)
    if args.command == "check":
        print(f"Valid {config.mode} configuration: {len(config.points)} points; {config.config_id}")
    elif args.command == "capacity":
        print(json.dumps(capacity(config, args.disk_bytes, args.cleanup_stop)))
    elif args.command == "init-accounts":
        import psycopg

        try:
            saved = init_accounts(Path(args.accounts), json.loads(Path(args.database).read_text())["primary"], config.timing)
        except psycopg.Error as error:
            raise SystemExit(
                f"The primary database cannot be used ({type(error).__name__}): the administrator goes into it and the account file together"
            ) from None
        if saved:
            print(f"Administrator created; its credential is in {saved}: hand it to the owner, who keeps it off this computer - "
                  "the initial password stays the fallback for good; never commit this file.")
        else:
            print("The administrator already exists; no changes.")
    else:
        with nullcontext() if deployment_locked else deployment_lock():
            _run(args, config)


def _run(args, config):
    import uvicorn

    from .archive import ArchiveStore
    from .archive_router import ArchiveRouter
    from .runtime import Runtime
    from .web import create_web_app

    databases = json.loads(Path(args.database).read_text())
    if "external" in databases:  # 修复轮08: the removable database is gone; a file naming it is out of date, not ignored
        raise SystemExit(f'{args.database} still names an "external" database; the removable database is no longer supported')
    accounts = Accounts(Path(args.accounts))  # unreadable or empty stops nothing: the runtime settles it
    stores = {name: ArchiveStore(databases[name], config.timing) for name in ("primary", "emergency")}
    router = ArchiveRouter(config, stores["primary"], stores["emergency"])
    runtime = Runtime(config, router, accounts, number_file=NUMBER_FILE)
    Path(".local").mkdir(exist_ok=True)
    handler = RotatingFileHandler(
        ".local/backend.log", maxBytes=2 * 1024**2, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    app = create_web_app(
        config=config, accounts=accounts, owner=runtime.owner, archive_router=router,
        queue=runtime.queue, diagnostics=runtime.diagnostics, lifecycle=runtime,
    )  # fmt: skip
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.port,
            workers=1,
            access_log=False,
            log_config=None,  # its reports go to the rotated backend.log, never to the console file nothing rotates
            limit_concurrency=32,
            timeout_graceful_shutdown=10,
        )
    )

    async def serve():
        serving = asyncio.create_task(server.serve())
        failed = asyncio.create_task(runtime.failed.wait())
        done, _ = await asyncio.wait([serving, failed], return_when=asyncio.FIRST_COMPLETED)
        if failed in done:
            server.should_exit = True
            await serving
            raise SystemExit("Core worker failed; supervisor should restart backend")
        failed.cancel()
        await serving

    asyncio.run(serve())


if __name__ == "__main__":
    main()
