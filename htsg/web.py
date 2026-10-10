"""The web application of the batch-driven chain (方案模块 G).

It only assembles parts that already exist: the pages, identity, the UI configuration, the realtime socket of
`stream_api` and the history routes of `history_api`.  The caller builds the owner, publisher, archive and
accounts and hands them in; nothing here reads a clock or touches a protocol.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles

from .history_api import build_history_router
from .stream_api import build_stream_router

STATIC = Path(__file__).with_name("static")


def create_web_app(*, config, accounts, publisher, archive_router=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(publisher.run())  # the realtime packing and heartbeat clock
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="HTSG", lifespan=lifespan)
    bearer = HTTPBearer(auto_error=False)
    auth_slots = asyncio.Semaphore(4)

    async def verify(token):
        if not isinstance(token, str) or len(token) > 256:
            return None
        if config.mode == "development" and config.dev_admin_login and token == "admin:admin":
            account = accounts.users.get("admin", {})
            if not accounts.uncertain and account.get("active") and account.get("role") == "admin":
                return {"username": "admin", "role": "admin"}
            return None
        async with auth_slots:  # scrypt is slow on purpose: a few at a time, off the event loop
            return await asyncio.to_thread(accounts.verify, token)

    async def user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
        result = await verify(credentials.credentials if credentials else "")
        if result is None:
            raise HTTPException(401, "Invalid credentials", headers={"WWW-Authenticate": "Bearer"})
        return result

    def account_ok(identity):
        return not accounts.uncertain and accounts.users.get(identity["username"], {}).get("active", False)

    @app.get("/live")
    async def live():
        return {"alive": True}

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC, check_dir=False), name="static")

    @app.get("/api/me")
    async def me(identity=Depends(user)):
        return identity

    @app.get("/api/ui-config", dependencies=[Depends(user)])
    async def ui_config():
        timing = config.timing
        return {
            "project": config.project.model_dump(),
            "ui": config.ui.model_dump(),
            "points": [p.model_dump() for p in config.points],
            "derived": [d.model_dump() for d in config.derived],
            "models": [m.model_dump() for m in config.models],
            "config_id": config.config_id,
            "mode": config.mode,
            "timing": {"push_heartbeat": timing.push_heartbeat, "ui_stall_notice": timing.ui_stall_notice},
        }

    app.include_router(build_stream_router(publisher, verify, account_ok, config))
    if archive_router is not None:
        app.include_router(build_history_router(archive_router, user, config))
    return app
