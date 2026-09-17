"""example-plugin's one endpoint — the menu-bar widget's data source.

A self-contained plugin (see docs/plugins.md) owns its whole vertical slice: its own
router, mounted by the loader at import (``ROUTER`` in ``__init__.py``), and its own
frontend layout that talks to it directly."""

from __future__ import annotations

from fastapi import APIRouter

from conductor.models import utcnow

router = APIRouter(prefix="/api/plugins/example-plugin", tags=["example-plugin"])


@router.get("/time")
async def time() -> dict:
    return {"server_time": utcnow().isoformat()}
