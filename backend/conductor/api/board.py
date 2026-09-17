from __future__ import annotations

from fastapi import APIRouter

from ..lanes import stage_registry

router = APIRouter(prefix="/api/board", tags=["board"])


@router.get("/lanes")
async def lanes() -> list[dict]:
    """The board's lane set (built-ins + ~/.conductor/lanes.json + plugin stages),
    display-ordered. The FE builds its columns/counters from this instead of a
    hardcoded list — adding a stage never touches frontend code."""
    return stage_registry()
