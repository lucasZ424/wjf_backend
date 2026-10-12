"""The realtime WebSocket (终版 §6, §11, 方案 D37).

One client, one bounded outbox.  A client that cannot keep up - its outbox overflows or one send takes longer
than `client_send_timeout` - is disconnected at once; it reconnects and takes a full snapshot.  It never gets a
patch-up of what it missed, and no other client and no archive write waits for it.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

AUTH_WAIT = 5.0  # seconds a new connection has to present its credential
POLICY, TRY_AGAIN = 1008, 1013


async def serve(ws: WebSocket, publisher, verify, account_ok, config):
    """`verify(token)` -> identity or None; `account_ok(identity)` is asked again before every message, so a
    deactivated account is cut off within one heartbeat."""
    origin = ws.headers.get("origin")
    own_origin = ("https" if ws.url.scheme == "wss" else "http") + "://" + ws.headers.get("host", "")
    if origin and origin != own_origin and origin not in config.allowed_origins:
        await ws.close(POLICY)
        return
    await ws.accept()
    client = None
    try:
        first = await asyncio.wait_for(ws.receive_json(), AUTH_WAIT)
        identity = await verify(first.get("credential", "") if isinstance(first, dict) else "")
        if not identity:
            await ws.close(POLICY)
            return
        client = publisher.connect()
        while True:
            message = await client.next()
            if message is None:  # increments were lost: only a new snapshot can make this client right again
                await ws.close(TRY_AGAIN)
                return
            if not account_ok(identity):  # asked before each send, so a deactivated account gets nothing more
                await ws.close(POLICY)
                return
            await asyncio.wait_for(ws.send_json(message), config.timing.client_send_timeout)
    except (WebSocketDisconnect, TimeoutError, ValueError, RuntimeError):
        try:
            await ws.close(TRY_AGAIN)
        except (RuntimeError, WebSocketDisconnect):  # the peer is already gone: nothing is left to tell it
            pass
    finally:
        if client:
            publisher.disconnect(client)


def build_stream_router(publisher, verify, account_ok, config) -> APIRouter:
    api = APIRouter()

    @api.websocket("/api/stream")
    async def stream(ws: WebSocket):
        await serve(ws, publisher, verify, account_ok, config)

    return api
