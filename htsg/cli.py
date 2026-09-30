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
        accounts = Accounts(Path(args.accounts))
        credentials = {}
        for name in ("admin", "viewer"):
            if name not in accounts.users:
                credentials[name] = accounts.create(name, name)
        if credentials:
            destination = Path(args.accounts).with_name("initial-credentials.json")
            destination.write_text(json.dumps(credentials), encoding="utf-8")
            print(f"Initial credentials saved to {destination}; never commit this file.")
        else:
            print("Accounts already initialized; no changes.")
    else:
        with nullcontext() if deployment_locked else deployment_lock():
            _run(args, config)


def _run(args, config):
    import uvicorn

    from .api import create_app
    from .runtime import Runtime
    from .storage import Router, Store

    databases = json.loads(Path(args.database).read_text())
    if config.storage.external_enabled and not databases.get("external"):
        raise SystemExit("Enabled external storage requires an external database binding")
    accounts = Accounts(Path(args.accounts))
    if not accounts.users:
        raise SystemExit("Run init-accounts first")
    external = Store(databases["external"]) if databases.get("external") else None
    runtime = Runtime(
        config, Router(config, Store(databases["primary"]), Store(databases["emergency"]), external)
    )
    Path(".local").mkdir(exist_ok=True)
    handler = RotatingFileHandler(
        ".local/backend.log", maxBytes=2 * 1024**2, backupCount=3, encoding="utf-8"
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(runtime, accounts),
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
