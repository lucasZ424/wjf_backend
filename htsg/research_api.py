"""Authenticated research UI; simulation control never injects observations."""

import asyncio
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field


def audit_entry(identity, action, target, result="COMMITTED"):
    return {
        "id": str(uuid4()),
        "time": time.time(),
        "actor": identity["username"],
        "action": action,
        "target": target,
        "result": result,
    }


class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scenario: str = Field(max_length=80)


def register_research_routes(app, runtime, user, admin):
    config = runtime.config
    static = Path(__file__).with_name("static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(static / "index.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=static, check_dir=False), name="static")

    @app.get("/api/me")
    async def me(identity=Depends(user)):
        return identity

    @app.get("/api/ui-config", dependencies=[Depends(user)])
    async def ui_config():
        return {
            "project": config.project.model_dump(),
            "ui": config.ui.model_dump(),
            "points": [p.model_dump() for p in config.points],
            "derived": [d.model_dump() for d in config.derived],
            "profiles": [p.model_dump() for p in config.profiles],
            "models": [m.model_dump() for m in config.models],
            "config_id": config.config_id,
            "simulation": {
                "enabled": config.simulation.enabled,
                "scenarios": [{"name": s.name, "label": s.label} for s in config.simulation.scenarios],
            },
        }

    @app.get("/api/communications", dependencies=[Depends(user)])
    async def communications():
        sources = []
        for source in config.sources:
            points = []
            for point in config.points:
                if point.source != source.id:
                    continue
                state = runtime.engine.states[point.id]
                sample = state.latest
                points.append(
                    {
                        "id": point.id,
                        "unit": point.unit,
                        "value": sample.value if sample and state.available else None,
                        "quality": sample.quality
                        if sample and (state.available or sample.quality != "GOOD")
                        else "UNAVAILABLE",
                        "raw_quality": sample.quality if sample else None,
                        "available": state.available,
                        "source_time": sample.source_time if sample else None,
                        "received_at": sample.received_at if sample else None,
                        "address": point.address,
                        "node_id": point.node_id,
                        "data_type": point.data_type,
                    }
                )
            received = runtime.source_receipts.get(source.id)
            fresh = received is not None and time.time() - received <= max(source.interval * 3, 5)
            connected = source.enabled and fresh and source.id not in runtime.source_errors
            sources.append(
                {
                    "id": source.id,
                    "kind": source.kind,
                    "connected": connected,
                    "health": runtime.source_errors.get(
                        source.id, "READING" if connected else "NO_RECENT_READ"
                    ),
                    "interval": source.interval,
                    "last_received_at": received,
                    "points": points,
                }
            )
        return {
            "sources": sources,
            "logs": list(reversed(runtime.communication_logs)),
            "connection_basis": "successful recent read, not a socket-level guarantee",
        }

    control_slots = asyncio.Semaphore(2)

    def control_request(scenario=None):
        sim = config.simulation
        token = os.environ.get(sim.token_env, "")
        if not token:
            raise HTTPException(503, "Simulation control credential unavailable")
        body = None if scenario is None else json.dumps({"scenario": scenario}).encode()
        request = Request(
            f"http://{sim.control_host}:{sim.control_port}/" + ("status" if body is None else "scenario"),
            data=body,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=3) as response:
                payload = response.read(65537)
                if len(payload) > 65536:
                    raise ValueError("oversized control response")
                return json.loads(payload)
        except HTTPError as error:
            raise HTTPException(502, "Simulation controller rejected request") from error
        except (URLError, TimeoutError, ValueError, OSError) as error:
            raise HTTPException(503, "Simulation result unknown; inspect status before retry") from error

    def enabled():
        if config.mode != "development" or not config.simulation.enabled:
            raise HTTPException(404, "Simulation disabled")

    @app.get("/api/simulation", dependencies=[Depends(user)])
    async def simulation_status():
        enabled()
        async with control_slots:
            return await asyncio.to_thread(control_request)

    @app.post("/api/simulation")
    async def simulation_change(body: ScenarioRequest, identity=Depends(admin)):
        enabled()
        if body.scenario not in {s.name for s in config.simulation.scenarios}:
            raise HTTPException(422, "Unknown configured scenario")
        async with control_slots:
            scenario = next(s for s in config.simulation.scenarios if s.name == body.scenario)
            if scenario.kind != "baseline" and (
                runtime.status()["history_archive"]["suspended"] or not runtime.accepting
            ):
                raise HTTPException(
                    409,
                    "归档当前不可用，无法保证记录本次试验。请先处理存储告警；开发库已满时可保留旧库并建立新实验库。",
                )
            # Persist intent first. A lost reply must not be reported as confirmed failure.
            await runtime.db_call(
                runtime.router.primary.audit, audit_entry(identity, "SCENARIO", body.scenario, "REQUESTED")
            )
            try:
                result = await asyncio.to_thread(control_request, body.scenario)
            except HTTPException:
                await runtime.db_call(
                    runtime.router.primary.audit, audit_entry(identity, "SCENARIO", body.scenario, "UNKNOWN")
                )
                raise
            await runtime.db_call(
                runtime.router.primary.audit, audit_entry(identity, "SCENARIO", body.scenario, "CONFIRMED")
            )
            return result

    @app.get("/api/admin/audit", dependencies=[Depends(admin)])
    async def audit_history():
        return await runtime.db_call(runtime.router.primary.audit_history)
