from __future__ import annotations

from fastapi import APIRouter

from . import metrics as metrics_actions
from conductor.config import settings

router = APIRouter(prefix="/api", tags=["metrics"])


@router.get("/metrics")
async def metrics() -> list[dict]:
    """Per-host system metrics for the Monitor tab. host=null is the local
    machine; entries carry the display name so the UI needn't special-case."""
    out = await metrics_actions.all_metrics()
    for d in out:
        d["name"] = d.get("host") or settings.local_host_name
    return out
