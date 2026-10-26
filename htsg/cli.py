import argparse
import asyncio
import json
import logging
from contextlib import contextmanager, nullcontext
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .auth import Accounts
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
    when the database or the file holds the administrator already.  Read-only users are added on the page."""
    from .archive import ArchiveStore
    from .web import audit_entry

    local, store = Accounts(accounts), ArchiveStore(primary_dsn, timing)
    try:
        local.persist = store.sync_accounts
        local.reconcile(store)  # the database copy first: it may hold the administrator already
        if any(user["role"] == "admin" for user in local.users.values()):
            return None
        credential = local.create("admin", "admin", audit_entry({"username": "init-accounts"}, "CREATE_ACCOUNT", "admin"))
    finally:
        store.close()
    destination = accounts.with_name("initial-credentials.json")
    destination.write_text(json.dumps({"admin": credential}), encoding="utf-8")
    return destination


def main(*, deployment_locked=False):
    parser = argparse.ArgumentParser(description="HTSG local monitoring backend")
    parser.add_argument("command", choices=["run", "check", "init-accounts", "capacity"])
    parser.add_argument("--config", default="config/development.json")
    parser.add_argument("--database", default=".local/database.json")
    parser.add_argument("--accounts", default=".local/accounts.json")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    config = load_settings(args.config)
    if args.command == "check":
        print(f"Valid {config.mode} configuration: {len(config.points)} points; {config.config_id}")
    elif args.command == "capacity":
        for bytes_per_sample in (128, 256, 512):
            rates = {s.id: 1 / s.interval for s in config.sources}
            total = sum(rates[p.source] for p in config.points)
            print(
                json.dumps(
                    {
                        "assumed_bytes_per_sample": bytes_per_sample,
                        "observations_per_second": total,
                        "continuous_capture_gib_per_year": total * bytes_per_sample * 365 * 86400 / 1024**3,
                        "note": "Scenario only; excludes WAL, indexes, frames and temporary growth",
                    }
                )
            )
    elif args.command == "init-accounts":
        import psycopg

        try:
            saved = init_accounts(Path(args.accounts), json.loads(Path(args.database).read_text())["primary"], config.timing)
        except psycopg.Error as error:
            raise SystemExit(
                f"The primary database cannot be used ({type(error).__name__}): the administrator goes into it and the account file together"
            ) from None
        if saved:
            print(f"Administrator created; its credential is in {saved}: hand it to the owner, never commit this file.")
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
    if config.storage.external_enabled and not databases.get("external"):
        raise SystemExit("Enabled external storage requires an external database binding")
    accounts = Accounts(Path(args.accounts))  # unreadable or empty stops nothing: the runtime settles it
    stores = {name: ArchiveStore(databases[name], config.timing) for name in ("primary", "emergency")}
    external = ArchiveStore(databases["external"], config.timing) if databases.get("external") else None
    router = ArchiveRouter(config, stores["primary"], stores["emergency"], external)
    runtime = Runtime(config, router, accounts)
    Path(".local").mkdir(exist_ok=True)
    handler = RotatingFileHandler(
        ".local/backend.log", maxBytes=2 * 1024**2, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    app = create_web_app(
        config=config, accounts=accounts, publisher=runtime.publisher, archive_router=router,
        queue=runtime.queue, diagnostics=runtime.diagnostics, lifecycle=runtime,
    )  # fmt: skip
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.port,
            workers=1,
            access_log=False,
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
