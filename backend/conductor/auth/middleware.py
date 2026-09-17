"""Session-cookie gate. Raw ASGI middleware (NOT BaseHTTPMiddleware) so it also
covers websocket scopes (/ws and the ttyd /term/{sid}/ws proxy).

Gating map when settings.auth_enabled is true:
  exempt : /api/auth/*  (login itself), /api/health, the per-card
           claude-hook posts /api/cards/{id}/agent + /notify, and every plugin's
           declared claude-hook endpoint (machine push hooks — bare curl, no session
           cookie; auth being ON silently 401'd them for 9 days, killing instant
           AI-Working flips and card notifications. Each is optionally guarded by
           CONDUCTOR_INGEST_TOKEN in its endpoint.)
  gated  : /ws, /api/*, /term/*
  open   : everything else (the static SPA — it renders the login screen)

Ordering (main.py): CORS must be OUTERMOST so middleware 401s still carry CORS
headers. Starlette's add_middleware prepends (last added = outermost), so
AuthMiddleware is added BEFORE CORSMiddleware. Final: CORS → Auth → routes."""

from __future__ import annotations

import json
import re
from typing import Any

from .. import db
from ..config import settings
from . import service

# machine push hooks (claude's per-card state/notification posts) — session-exempt,
# token-guarded in their own endpoint (see settings.ingest_token)
_HOOK_PATH_RE = re.compile(r"^/api/cards/[^/]+/(?:agent|notify)$")

# plugin claude-hook endpoints (from every plugin's HookSpec.path) are machine push
# hooks too — same bare-curl/no-cookie shape, token-guarded in the plugin's own
# endpoint. Collected from the kernel's hook registrations so a new plugin hook is
# exempt with no edit here (the whole point of the generic hook framework). Computed
# once, lazily: a top-level plugins import would cycle, and registrations are static
# after discovery. The cache DELIBERATELY stays: exemption must match exactly and
# cheaply on every request, and a kernel-native hook registered after boot should
# not silently widen the auth exemption set mid-flight.
_plugin_hook_paths: frozenset[str] | None = None


def _hook_paths() -> frozenset[str]:
    global _plugin_hook_paths
    if _plugin_hook_paths is None:
        from ..plugins import runtime

        _plugin_hook_paths = frozenset(
            spec.path for _plugin_id, spec in runtime.spec_rows("hooks")
        )
    return _plugin_hook_paths


def _session_cookie(scope: dict[str, Any]) -> str:
    """Extract the conductor_session cookie straight from raw ASGI headers."""
    for name, value in scope.get("headers") or []:
        if name != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            key, _, val = part.strip().partition("=")
            if key == service.SESSION_COOKIE:
                return val
    return ""


class AuthMiddleware:
    def __init__(self, app) -> None:  # noqa: ANN001 — ASGI app
        self.app = app

    async def __call__(self, scope, receive, send) -> None:  # noqa: ANN001 — ASGI signature
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        if not settings.auth_enabled:
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        # /api/health stays open: probes (launchd, uptime checks, curl) must be able
        # to tell "up" from "down" without a session — a 401 health check is useless.
        if (
            path.startswith("/api/auth/")
            or path == "/api/health"
            or _HOOK_PATH_RE.match(path)
            or path in _hook_paths()
        ):
            await self.app(scope, receive, send)
            return
        gated = (
            path == "/ws"
            or path.startswith("/api/")
            or path.startswith("/term/")
        )
        if not gated:
            await self.app(scope, receive, send)
            return
        raw = _session_cookie(scope)
        user = None
        if raw:
            async with db.session_maker() as session:
                user = await service.validate_session_token(session, raw)
        if user is not None:
            state = scope.setdefault("state", {})
            state["user"] = user
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            # close WITHOUT accepting → handshake rejected with policy code 4401
            await send({"type": "websocket.close", "code": 4401})
            return
        body = json.dumps({"detail": "authentication required"}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
