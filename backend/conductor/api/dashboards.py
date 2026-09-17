from __future__ import annotations

import json

from fastapi import APIRouter

from ..config import settings

router = APIRouter(prefix="/api", tags=["dashboards"])


@router.get("/dashboards")
async def list_dashboards() -> list[dict]:
    """Custom personal boards, read from a LOCAL file (CONDUCTOR_DASHBOARDS_FILE,
    default ~/.conductor/dashboards.json — OUTSIDE the repo) so each user defines their
    own without touching shared code. Each entry: {id, label, icon?}. A manual card
    tagged cached.manual.board == <id> shows on that board and is kept off the main work
    board. Absent/invalid file → no extra boards (teammates just get the standard board).

    An entry may also carry ``origins``: plugin card origins (e.g. "visualagent")
    whose cards ALL show on that board — how a plugin source joins a dashboard
    without each card being manual.

    Example ~/.conductor/dashboards.json:
      [{"id": "personal", "label": "Personal", "icon": "📝", "origins": ["visualagent"]}]
    """
    try:
        data = json.loads(settings.dashboards_file.read_text())
    except (FileNotFoundError, ValueError, OSError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for d in data:
        if isinstance(d, dict) and d.get("id") and d.get("label"):
            raw_origins = d.get("origins")
            origins = (
                [str(o) for o in raw_origins if o] if isinstance(raw_origins, list) else []
            )
            out.append(
                {
                    "id": str(d["id"]),
                    "label": str(d["label"]),
                    "icon": str(d.get("icon") or "▦"),
                    "origins": origins,
                }
            )
    return out
