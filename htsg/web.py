"""The web application of the batch-driven chain (方案模块 G).

It only assembles parts that already exist: the pages, identity, the UI configuration, the realtime socket of
`stream_api` and the history routes of `history_api`.  The caller builds the owner, publisher, archive and
accounts and hands them in; nothing here reads a clock or touches a protocol.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict

from .history_api import build_history_router
from .stream_api import build_stream_router

STATIC = Path(__file__).with_name("static")


class NewAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    role: str


def audit_entry(identity, action, target, result="COMMITTED"):
    return {"id": str(uuid4()), "time": time.time(), "actor": identity["username"], "action": action, "target": target, "result": result}


def create_web_app(*, config, accounts, publisher, archive_router=None, queue=None, diagnostics=None) -> FastAPI:
    """`queue` is the archive writer queue (for its counters); `diagnostics()` returns what only the acquisition
    side knows, e.g. the OPC UA parameters the server really granted (`ua.Negotiated.as_dict()` per source)."""
    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(publisher.run())  # the realtime packing and heartbeat clock
        try:
            yield
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    app = FastAPI(title="HTSG", lifespan=lifespan)

    @app.exception_handler(psycopg.Error)
    async def database_failure(request, error):
        return JSONResponse(status_code=503, content={"detail": "Database operation failed; change not confirmed"})

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

    async def admin(identity=Depends(user)):
        if identity["role"] != "admin":
            raise HTTPException(403, "Administrator required")
        return identity

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
            "timing": {
                name: getattr(timing, name)
                for name in (
                    "push_heartbeat", "ui_stall_notice", "push_coalesce", "ua_data_validity", "ua_alarm_seconds",
                    "modbus_alarm_seconds", "fault_confirm_seconds", "fault_buffer_limit", "watermark_interval",
                )
            },
        }

    @app.get("/api/health", dependencies=[Depends(user)])
    async def health():
        """What the self-check page shows besides the live stream: archive, writer queue, negotiated parameters."""
        owner = publisher.owner
        archive = {"sources": {sid: owner.history_status(sid) for sid in owner.sources}, "routing": None, "health": {}}
        if archive_router is not None:
            archive["routing"], archive["health"] = archive_router.mode, dict(archive_router.health)
        if queue is not None:
            archive["queue"] = {"depth": queue.depth, "capacity": queue.capacity, **queue.stats}
        return {"status": owner.status(), "archive": archive, "negotiated": diagnostics() if diagnostics else {}}

    @app.get("/api/admin/accounts", dependencies=[Depends(admin)])
    async def list_accounts():
        return [{"username": name, "role": u["role"], "active": u["active"]} for name, u in accounts.users.items()]

    @app.post("/api/admin/accounts", status_code=201)
    async def add_account(body: NewAccount, identity=Depends(admin)):
        try:
            token = await asyncio.to_thread(accounts.create, body.username, body.role, audit_entry(identity, "CREATE_ACCOUNT", body.username))
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"credential": token, "note": "Displayed only at creation"}

    @app.delete("/api/admin/accounts/{username}")
    async def revoke(username: str, identity=Depends(admin)):
        try:
            await asyncio.to_thread(accounts.revoke, username, audit_entry(identity, "REVOKE_ACCOUNT", username))
        except ValueError as error:
            raise HTTPException(409, str(error)) from error
        return {"revoked": username}

    @app.get("/api/admin/audit", dependencies=[Depends(admin)])
    async def audit_history():
        if archive_router is None:
            raise HTTPException(503, "No archive: management records are not kept")
        return await asyncio.to_thread(archive_router.primary.audit_history)

    app.include_router(build_stream_router(publisher, verify, account_ok, config))
    if archive_router is not None:
        app.include_router(build_history_router(archive_router, user, config))
    return app
