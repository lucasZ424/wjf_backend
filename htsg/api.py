import asyncio
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict

from .auth import Accounts
from .runtime import Runtime


def create_app(runtime: Runtime, accounts: Accounts):
    @asynccontextmanager
    async def lifespan(app):
        await runtime.start()
        accounts.persist = runtime.router.primary.sync_accounts
        try:
            try:
                await runtime.db_call(accounts.reconcile, runtime.router.primary)
                runtime.health["accounts"] = "DATABASE_AUTHORITATIVE"
            except psycopg.Error:
                if accounts.uncertain:
                    runtime.health["accounts"] = "RECONCILIATION_REQUIRED"
                    raise
                runtime.health["accounts"] = "LOCAL_VERIFICATION_CACHE_ONLY"
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="HTSG Backend", version="0.2.2", lifespan=lifespan)
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime.config.allowed_origins),
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
    import psycopg
    from fastapi.responses import JSONResponse

    @app.exception_handler(psycopg.Error)
    async def database_failure(request, error):
        return JSONResponse(
            status_code=503, content={"detail": "Database operation failed; change not confirmed"}
        )

    bearer = HTTPBearer(auto_error=False)
    auth_slots = asyncio.Semaphore(4)

    async def verify(token):
        if not isinstance(token, str) or len(token) > 256:
            return None
        if runtime.config.mode == "development" and runtime.config.dev_admin_login and token == "admin:admin":
            account = accounts.users.get("admin", {})
            if not accounts.uncertain and account.get("active") and account.get("role") == "admin":
                return {"username": "admin", "role": "admin"}
            return None
        async with auth_slots:
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

    @app.get("/live")
    def live():
        if runtime.failed.is_set():
            raise HTTPException(503, "Worker failed")
        return {"alive": True}

    @app.get("/api/current", dependencies=[Depends(user)])
    async def current():
        return runtime.snapshot()

    @app.get("/api/health", dependencies=[Depends(user)])
    async def health():
        return runtime.status()

    @app.get("/api/points", dependencies=[Depends(user)])
    async def points():
        return {
            "points": [
                {"id": p.id, "unit": p.unit, "aggregation": p.aggregation} for p in runtime.config.points
            ],
            "derived": [
                {"id": d.id, "unit": d.unit, "configured": d.operation != "unconfigured"}
                for d in runtime.config.derived
            ],
        }

    async def query(category, **kwargs):
        try:
            result = await runtime.db_call(runtime.router.merged, category, **kwargs)
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if len(result["unavailable_instances"]) == len(runtime.router.stores):
            raise HTTPException(503, "Historical storage unavailable")
        return result

    @app.get("/api/history", dependencies=[Depends(user)])
    async def history(
        start: float,
        end: float,
        point: str | None = None,
        event_id: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0, le=1000),
        cursor: str | None = Query(None, max_length=2048),
    ):
        if not start < end or end - start > 366 * 86400:
            raise HTTPException(422, "Require increasing range no longer than 366 days")
        return await query(
            "history",
            start=start,
            end=end,
            point=point,
            event_id=event_id,
            limit=limit,
            offset=offset,
            cursor=cursor,
        )

    @app.get("/api/events", dependencies=[Depends(user)])
    async def events(
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0, le=1000),
        cursor: str | None = Query(None, max_length=2048),
    ):
        return await query("events", limit=limit, offset=offset, cursor=cursor)

    @app.get("/api/faults", dependencies=[Depends(user)])
    async def faults(
        point: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, max_length=2048),
    ):
        return await query("faults", point=point, limit=limit, cursor=cursor)

    @app.get("/api/replay", dependencies=[Depends(user)])
    async def replay(
        center: float = Query(allow_inf_nan=False),
        seconds: float = Query(gt=0, le=366 * 86400, allow_inf_nan=False),
        point: str | None = None,
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, max_length=2048),
    ):
        start, end = center - seconds / 2, center + seconds / 2
        result = await query("replay", start=start, end=end, point=point, limit=limit, cursor=cursor)
        return {**result, "center": center, "seconds": seconds, "start": start, "end": end}

    @app.get("/api/events/{event_id}", dependencies=[Depends(user)])
    async def event(event_id: str):
        result = await query("event", event_id=event_id)
        if not result["items"] and not result["partial"]:
            raise HTTPException(404, "Event not found")
        return result

    @app.get("/api/events/{event_id}/{product}", dependencies=[Depends(user)])
    async def event_product(
        event_id: str,
        product: str,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0, le=1000),
        cursor: str | None = Query(None, max_length=2048),
    ):
        if product not in ("samples", "triggers", "gaps", "products"):
            raise HTTPException(404, "Unknown product")
        return await query(product, event_id=event_id, limit=limit, offset=offset, cursor=cursor)

    @app.get("/api/gaps", dependencies=[Depends(user)])
    async def gaps(
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0, le=1000),
        cursor: str | None = Query(None, max_length=2048),
    ):
        return await query("gaps", limit=limit, offset=offset, cursor=cursor)

    class NewAccount(BaseModel):
        model_config = ConfigDict(extra="forbid")
        username: str
        role: str

    @app.get("/api/admin/accounts", dependencies=[Depends(admin)])
    async def list_accounts():
        return [
            {"username": name, "role": u["role"], "active": u["active"]} for name, u in accounts.users.items()
        ]

    @app.post("/api/admin/accounts", status_code=201)
    async def add_account(body: NewAccount, identity=Depends(admin)):
        try:
            token = await asyncio.to_thread(
                accounts.create,
                body.username,
                body.role,
                audit_entry(identity, "CREATE_ACCOUNT", body.username),
            )
            return {"credential": token, "note": "Displayed only at creation"}
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    @app.delete("/api/admin/accounts/{username}")
    async def revoke(username: str, identity=Depends(admin)):
        try:
            await asyncio.to_thread(
                accounts.revoke, username, audit_entry(identity, "REVOKE_ACCOUNT", username)
            )
            return {"revoked": username}
        except ValueError as error:
            raise HTTPException(409, str(error)) from error

    class Observation(BaseModel):
        model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
        point_id: str
        value: float | None
        quality: str = "GOOD"
        source_time: float | None = None

    @app.post("/api/development/observations", dependencies=[Depends(admin)])
    async def inject(body: Observation):
        if runtime.config.mode != "development":
            raise HTTPException(404, "Not available")
        if body.point_id not in runtime.engine.points:
            raise HTTPException(422, "Unknown point")
        if not runtime.submit(body.point_id, body.value, body.quality, body.source_time):
            raise HTTPException(429, "Ingress full")
        return {"accepted": True}

    @app.websocket("/api/stream")
    async def stream(ws: WebSocket):
        origin = ws.headers.get("origin")
        own_origin = ("https" if ws.url.scheme == "wss" else "http") + "://" + ws.headers.get("host", "")
        if origin and origin != own_origin and origin not in runtime.config.allowed_origins:
            await ws.close(1008)
            return
        await ws.accept()
        subscriber = None
        try:
            first = await asyncio.wait_for(ws.receive_json(), 5)
            token = first.get("credential", "") if isinstance(first, dict) else ""
            identity = await verify(token)
            if not identity:
                await ws.close(1008)
                return
            subscriber = runtime.subscribe()
            while True:
                if accounts.uncertain or not accounts.users.get(identity["username"], {}).get(
                    "active", False
                ):
                    await ws.close(1008)
                    return
                message = await subscriber.next()
                if accounts.uncertain or not accounts.users.get(identity["username"], {}).get(
                    "active", False
                ):
                    await ws.close(1008)
                    return
                await asyncio.wait_for(ws.send_json(message), 5)
                if message["type"] == "resync_required":
                    await ws.close(1013)
                    return
        except (WebSocketDisconnect, TimeoutError, ValueError, RuntimeError):
            try:
                await ws.close(1013)
            except RuntimeError:
                pass
        finally:
            if subscriber:
                runtime.subscribers.discard(subscriber)

    from .research_api import audit_entry, register_research_routes

    register_research_routes(app, runtime, user, admin)
    return app
