from __future__ import annotations

import asyncio

import httpx
import websockets
from fastapi import APIRouter, Depends, HTTPException, Request, Response, WebSocket
from fastapi.responses import RedirectResponse
from sqlmodel.ext.asyncio.session import AsyncSession

from ..actions import terminal as term
from ..config import settings
from ..db import get_session, session_maker
from ..models import TerminalSession

# Reverse-proxy ttyd (which binds 127.0.0.1 under base-path /term/<id>) so the
# embedded terminal is same-origin as the dashboard — works locally AND through
# `tailscale serve` (one HTTPS origin). No /api prefix: routes live at /term/*.
router = APIRouter(tags=["term-proxy"])


async def _port_for(session_id: str, session: AsyncSession) -> int | None:
    port = term.port_for(session_id)
    if port:
        return port
    ts = await session.get(TerminalSession, session_id)
    return ts.ttyd_port if ts and ts.status == "live" else None


@router.get("/term/{sid}")
async def term_root(sid: str):
    return RedirectResponse(url=f"/term/{sid}/")


@router.websocket("/term/{sid}/ws")
async def term_ws(websocket: WebSocket, sid: str) -> None:
    async with session_maker() as session:
        port = await _port_for(sid, session)
    if not port:
        await websocket.close(code=1011)
        return

    requested = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol="tty" if "tty" in requested else None)

    upstream = f"ws://127.0.0.1:{port}/term/{sid}/ws"
    try:
        async with websockets.connect(
            upstream, subprotocols=["tty"], max_size=None, ping_interval=None
        ) as up:

            async def client_to_upstream() -> None:
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                        if msg.get("bytes") is not None:
                            await up.send(msg["bytes"])
                        elif msg.get("text") is not None:
                            await up.send(msg["text"])
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    await up.close()

            async def upstream_to_client() -> None:
                try:
                    async for m in up:
                        if isinstance(m, (bytes, bytearray)):
                            await websocket.send_bytes(bytes(m))
                        else:
                            await websocket.send_text(m)
                except Exception:  # noqa: BLE001
                    pass

            await asyncio.gather(client_to_upstream(), upstream_to_client())
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass


@router.get("/term/{sid}/{path:path}")
async def term_http(
    sid: str, path: str, request: Request, session: AsyncSession = Depends(get_session)
) -> Response:
    port = await _port_for(sid, session)
    if not port:
        raise HTTPException(status_code=404, detail="terminal not found")
    url = f"http://127.0.0.1:{port}/term/{sid}/{path}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            up = await client.get(url, params=dict(request.query_params))
    except httpx.HTTPError:
        # the ttyd viewer is gone (killed on a backend restart/crash) while a stale
        # 'live' row still points at its dead port — surface it cleanly instead of an
        # uncaught 500 "Internal Server Error"; close + reopen spins up a fresh one.
        return Response(
            content=(
                "<body style='margin:0;background:#000;color:#888;font:13px monospace;"
                "padding:1rem'>terminal ended — close and reopen it</body>"
            ),
            status_code=502,
            media_type="text/html",
        )
    return Response(
        content=up.content,
        status_code=up.status_code,
        media_type=up.headers.get("content-type"),
    )
