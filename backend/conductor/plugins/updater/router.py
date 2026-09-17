"""Updater endpoints, mounted by the plugin loader (see plugins/__init__.py)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from ...auth import service as auth_service
from . import service

router = APIRouter(prefix="/api/update", tags=["updater"])


def _same_origin(request: Request) -> None:
    """CSRF gate for the mutating endpoints. Both take no body and no custom header, so they
    are CORS *simple* requests: any page the browser visits while Conductor is running could
    fire them cross-site, and `auth_enabled` defaults to False so AuthMiddleware waves them
    through. /apply's side effect is "pull, run the new lockfiles' install scripts, restart",
    which is not something a foreign page gets to trigger. Browsers always send `Origin` on a
    non-GET fetch, including a same-origin one, so the widget is unaffected and a foreign page
    gets a 403. (GET /status stays open: it is a read, and same-origin GETs carry no Origin.)"""
    auth_service.require_origin(request)


@router.get("/status")
async def get_status() -> dict:
    """Cached update status (no network) — what the header widget polls."""
    return await service.current()


@router.post("/check")
async def check_now(request: Request) -> dict:
    """Force a `git fetch` + recompute now — the widget's "check now" button. A failed
    fetch raises inside refresh; hand back the cached status (with fetch_error set) rather
    than a 500, so the widget can say "couldn't reach the remote" and keep the last state."""
    _same_origin(request)
    try:
        return await service.refresh()
    except Exception:  # noqa: BLE001 — fetch failure: surface it via the cached status
        return await service.current()


@router.post("/apply")
async def apply(request: Request) -> dict:
    """Fast-forward the checkout and restart the backend (detached)."""
    _same_origin(request)
    return await service.apply()
