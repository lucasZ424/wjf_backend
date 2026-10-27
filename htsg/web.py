"""The web application of the batch-driven chain (方案模块 G).

It only assembles parts that already exist: the pages, identity, the UI configuration, the owner's current state
for the live pages and the history routes of `history_api`.  The caller builds the owner, archive and accounts and
hands them in; nothing here touches a protocol.
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

from . import history
from .history_api import build_history_router

STATIC = Path(__file__).with_name("static")


class NewAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str
    role: str


class NewSecret(BaseModel):
    model_config = ConfigDict(extra="forbid")
    secret: str


def audit_entry(identity, action, target, result="COMMITTED"):
    return {"id": str(uuid4()), "time": time.time(), "actor": identity["username"], "action": action, "target": target, "result": result}


def _object(description, properties=None, **extra):
    return {"type": "object", "description": description, **({"properties": properties} if properties else {}), **extra}


_NUMBER, _TEXT, _FLAG, _COUNT = {"type": "number"}, {"type": "string"}, {"type": "boolean"}, {"type": "integer"}
_NUMBER_N, _TEXT_N, _COUNT_N = ({"type": [t, "null"]} for t in ("number", "string", "integer"))  # OpenAPI 3.1: a type or null
HEALTH_SCHEMA = _object(
    "Memory only: nothing here reads the database or asks a device.",
    {
        "status": _object(
            "The owner's source and fault state; each source also carries the acquirer's `connect_failures`.",
            {
                "sources": _object(
                    "Per source id.",
                    additionalProperties=_object(
                        "State, alarm, archive state and last communication of one source.",
                        {
                            "resync_count": {**_COUNT, "description": "Entries into RESYNC since the last stable spell of valid communication"},
                            "persistent_resync": {**_FLAG, "description": "resync_count reached the persistent threshold (5)"},
                            "connect_failures": {**_COUNT, "description": "Failed connect attempts since the public connect last succeeded (0: none yet)"},
                        },
                    ),
                )
            },
        ),
        "archive": _object("Archive routing, writer queue and per-source archive state."),
        "negotiated": _object("What the OPC UA server really granted, per source (unchanged shape)."),
        "runtime": _object("Runtime flags."),
        "communication": _object(
            "What the communication page shows.",
            {
                "taken_time": {**_NUMBER, "description": "When this response was taken from the owner; not an observation time"},
                "log": _object(
                    "This run's communication log, in memory, at most `limit` entries, oldest first.",
                    {
                        "scope": _TEXT,
                        "limit": _COUNT,
                        "evicted": {**_COUNT, "description": "Entries moved out of the log (first in, first out)"},
                        "oldest_time": {**_NUMBER_N, "description": "Discovery time of the oldest entry still held"},
                        "diag_lost": {**_COUNT, "description": "Diagnostic facts refused at the ordered entry; not a data loss"},
                        "incomplete": _FLAG,
                        "entries": {
                            "type": "array",
                            "items": _object(
                                "One log entry; `state` is ACTIVE / ENDED for repeated failures, empty for a single event.",
                                {
                                    "seq": _COUNT, "source_id": _TEXT, "kind": _TEXT, "event": _TEXT, "reason": _TEXT,
                                    "conn_gen": {**_COUNT_N}, "phase": {**_TEXT_N},
                                    "detail": {**_TEXT_N}, "detail_truncated": _FLAG,
                                    "found_time": _NUMBER, "last_comm_time": {**_NUMBER_N},
                                    "first_failure": {**_NUMBER_N}, "last_failure": {**_NUMBER_N},
                                    "count": _COUNT, "state": {**_TEXT_N},
                                    "ended_time": {**_NUMBER_N}, "end_reason": {**_TEXT_N},
                                },
                            ),
                        },
                    },
                ),
                "inputs": {
                    "type": "array",
                    "description": "Every input, one row each; a row is taken whole from ONE observation.",
                    "items": _object(
                        "observation_id, value, quality, status_code, received_time and source_time all belong to the same observation.",
                        {
                            "point_id": _TEXT, "source_id": _TEXT, "observation_id": {**_TEXT_N},
                            "value": {**_NUMBER_N}, "quality": {**_TEXT_N},
                            "status_code": {**_COUNT_N}, "received_time": {**_NUMBER_N},
                            "source_time": {**_NUMBER_N, "description": "As the server gave it (OPC UA only); judges nothing"},
                            "valid": {**_FLAG, "description": "Validity at the moment of the response"}, "reason": {**_TEXT_N},
                        },
                    ),
                },
            },
        ),
    },
)


SNAPSHOT_SCHEMA = _object(
    "The whole current state, taken in one synchronous read of the owner (终版 §13.3); memory only, never cached.",
    {
        "taken_time": {**_NUMBER, "description": "When this reading was taken from the owner (the live trend's time); not an observation time"},
        "run_id": _TEXT,
        "channels": _object(
            "Per channel id: value, quality, validity, observation time and fault state, as the page must show them.",
            additionalProperties=_object(
                "One channel; an input also names its current observation.",
                {
                    "kind": _TEXT, "value": {**_NUMBER_N}, "valid": _FLAG, "time": {**_NUMBER_N},
                    "observation_id": {**_TEXT_N, "description": "Inputs only: the observation shown, so a new sample is told from one already shown"},
                    "origin": {**_TEXT_N, "description": "Inputs only: a receipt or a restatement of an earlier observation"},
                },
            ),
        ),
        "status": _object("The owner's source and fault state."),
    },
)


def create_web_app(*, config, accounts, owner, archive_router=None, queue=None, diagnostics=None, lifecycle=None) -> FastAPI:
    """`queue` is the archive writer queue (for its counters); `diagnostics()` returns what only the acquisition
    side knows, e.g. the OPC UA parameters the server really granted (`ua.Negotiated.as_dict()` per source).
    `lifecycle` (the `Runtime`) is started before the application takes requests and stopped after it has stopped."""
    @asynccontextmanager
    async def lifespan(app):
        if lifecycle is not None:
            await lifecycle.start()
        try:
            yield
        finally:
            if lifecycle is not None:
                await lifecycle.stop()

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

    limited = [x.id for x in [*config.points, *config.derived] if x.high is not None or x.low is not None]
    replay = {  # 修复轮03: what a fault brings to the history page, from the configuration as it was started
        "max_channels": history.CHANNEL_LIMIT,
        "default_seconds": history.DEFAULT_WINDOW_SECONDS,
        "templates": {channel: history.replay_template(config, channel) for channel in limited},
    }

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
            "config_id": config.config_id,
            "mode": config.mode,
            "replay": replay,
            "timing": {
                name: getattr(timing, name)
                for name in (
                    "ui_poll_interval", "ui_poll_timeout", "ui_stall_notice", "ua_data_validity", "ua_alarm_seconds",
                    "modbus_alarm_seconds", "fault_confirm_seconds", "fault_buffer_limit", "watermark_interval",
                )
            },
        }

    @app.get(
        "/api/snapshot", dependencies=[Depends(user)],
        responses={200: {"description": "The whole current state", "content": {"application/json": {"schema": SNAPSHOT_SCHEMA}}}},
    )  # fmt: skip
    async def snapshot():
        """What the live pages show (修复轮05): the owner's whole current state, read at once on the event loop - no
        await between its parts, no database, no waiting for new data.  Never cached: every reading is a new one."""
        body = {"taken_time": time.time(), **owner.snapshot()}
        return JSONResponse(body, headers={"Cache-Control": "no-store"})  # serialized as it is, not walked field by field

    @app.get(
        "/api/health", dependencies=[Depends(user)],
        responses={200: {"description": "Health and communication state", "content": {"application/json": {"schema": HEALTH_SCHEMA}}}},
    )  # fmt: skip
    async def health():
        """What the self-check and communication pages show besides the live state: archive, writer queue, negotiated
        parameters, each source's resync and connect-failure counts, the run's communication log and the input table."""
        archive = {"sources": {sid: owner.history_status(sid) for sid in owner.sources}, "routing": None, "health": {}}
        if archive_router is not None:
            archive["routing"], archive["health"] = archive_router.mode, dict(archive_router.health)
        if queue is not None:
            archive["queue"] = {"depth": queue.depth, "capacity": queue.capacity, **queue.stats}
        runtime = dict(lifecycle.health) if lifecycle is not None else {}
        status, failures = owner.status(), lifecycle.connect_failures() if lifecycle is not None else {}
        for source_id, source in status["sources"].items():
            source["connect_failures"] = failures.get(source_id, 0)  # the acquirer's own count, copied
        communication = {"taken_time": time.time(), "log": owner.commlog.snapshot(), "inputs": owner.communication_inputs()}
        return {
            "status": status, "archive": archive, "negotiated": diagnostics() if diagnostics else {}, "runtime": runtime,
            "communication": communication,
        }  # fmt: skip

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

    @app.post("/api/admin/password")
    async def set_password(body: NewSecret, identity=Depends(admin)):
        """The administrator's own password (修复轮02): the credential in use stops working."""
        try:
            await asyncio.to_thread(accounts.set_secret, identity["username"], body.secret, audit_entry(identity, "SET_PASSWORD", identity["username"]))
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        return {"changed": identity["username"]}

    @app.get("/api/admin/audit", dependencies=[Depends(admin)])
    async def audit_history():
        if archive_router is None:
            raise HTTPException(503, "No archive: management records are not kept")
        return await asyncio.to_thread(archive_router.primary.audit_history)

    if archive_router is not None:
        app.include_router(build_history_router(archive_router, user, config))
    return app
