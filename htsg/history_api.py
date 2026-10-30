"""Read-only HTTP surface of history replay and the fault list (终版 §14, 方案 D35).

Only GET routes exist: there is deliberately no way to add, remove or change a channel at run time - the
point table changes by editing the deployment configuration and restarting.  Reads go to the typed-record
archive only, so legacy frame / capture products can never leak into an answer.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from . import history

MAX_WINDOW_SECONDS = 366 * 86400


def parse_instant(value: str) -> float:
    """UTC epoch seconds, or an ISO-8601 instant WITH an offset; the instant is what counts, not its notation."""
    try:
        seconds = float(value)
    except ValueError:
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise HTTPException(422, "center must be UTC epoch seconds or ISO-8601 with an offset") from None
        if moment.tzinfo is None:
            raise HTTPException(422, "an ISO-8601 center needs an explicit UTC offset")
        seconds = moment.timestamp()
    if not math.isfinite(seconds):
        raise HTTPException(422, "center must be finite")
    return seconds


def build_history_router(router, user_dep, config, *, concurrency: int = 2) -> APIRouter:
    api = APIRouter()
    sources = history.channel_sources(config)
    units = {**{p.id: p.unit for p in config.points}, **{d.id: d.unit for d in config.derived}}
    derived = {d.id for d in config.derived}
    slots = asyncio.Semaphore(concurrency)

    async def blocking(func, *args, **kwargs):
        async with slots:  # database reads are synchronous; keep a few threads, never one per request
            return await asyncio.to_thread(func, *args, **kwargs)

    def all_unreadable(result):
        return len(result["unavailable"]) == len(router.stores)

    @api.get("/api/channels", dependencies=[Depends(user_dep)])
    async def channels():
        return {
            "channels": [
                {"id": cid, "unit": units.get(cid), "source_id": src, "kind": "derived" if cid in derived else "input"}
                for cid, src in sources.items()
            ]
        }

    @api.get("/api/faults", dependencies=[Depends(user_dep)])
    async def faults(
        point: str | None = None,
        direction: str | None = Query(None, pattern="^(HIGH|LOW)$"),
        source: str | None = None,
        start: float | None = None,
        end: float | None = None,
        limit: int = Query(100, ge=1, le=1000),
        cursor: str | None = Query(None, max_length=2048),
    ):
        try:
            result = await blocking(
                history.list_faults, router.stores, point=point, direction=direction, source=source,
                start=start, end=end, limit=limit, cursor=cursor,
            )  # fmt: skip
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        if all_unreadable({"unavailable": result["unavailable"]}):
            raise HTTPException(503, "Historical storage unavailable")
        return result

    @api.get("/api/replay", dependencies=[Depends(user_dep)])
    async def replay(
        center: str,
        seconds: float = Query(gt=0, le=MAX_WINDOW_SECONDS, allow_inf_nan=False),
        points: str = Query(min_length=1, max_length=20000),
        limit: int = Query(history.ROW_LIMIT, ge=1, le=history.ROW_LIMIT),
    ):
        chosen = list(dict.fromkeys(x for x in points.split(",") if x))
        unknown = [x for x in chosen if x not in sources]
        if not chosen or unknown:
            raise HTTPException(422, f"unknown channels: {unknown[:5]}" if unknown else "no channels given")
        if len(chosen) > history.CHANNEL_LIMIT:  # 修复轮03: a replay is bounded by construction, whatever is sent
            raise HTTPException(422, f"at most {history.CHANNEL_LIMIT} channels per replay")
        middle = parse_instant(center)
        start, end = middle - seconds / 2, middle + seconds / 2  # half a window either side of the centre
        result = await blocking(
            history.query, router.stores, {c: sources[c] for c in chosen}, start, end, limit=limit
        )
        if all_unreadable(result):
            raise HTTPException(503, "Historical storage unavailable")
        known = [v for v in result["coverage_end"].values()]
        result["coverage_complete_until"] = None if (not known or any(v is None for v in known)) else min(known)
        result["center"], result["seconds"] = middle, seconds
        for cid, channel in result["channels"].items():
            channel["unit"] = units.get(cid)
        return result

    @api.get("/api/replay/basis/{result_id:path}", dependencies=[Depends(user_dep)])
    async def basis(result_id: str, bucket: int | None = None):  # bucket: the one the result's history row names
        found = await blocking(history.input_basis, router.stores, result_id, bucket)
        if not found["found"]:
            if found["unavailable"]:
                raise HTTPException(503, "Historical storage unavailable")
            raise HTTPException(404, "Result not found")
        return found

    return api
