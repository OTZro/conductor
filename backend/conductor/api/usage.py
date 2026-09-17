from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["usage"])

# The statusline persists Claude Code's rate-limit snapshot here on every active
# session render (see statusline-command.sh). Account-wide, so this one file — kept
# fresh by the always-running conductor sessions — is authoritative.
_RL = Path.home() / ".claude" / "rate-limits.json"


def _norm(x: object) -> dict | None:
    if not isinstance(x, dict):
        return None
    return {"used_percentage": x.get("used_percentage"), "resets_at": x.get("resets_at")}


@router.get("/usage")
async def usage() -> dict:
    """Claude Code session (5h) + weekly (7d) usage with reset times. null fields
    until a session has reported (fresh install / no claude.ai subscription).
    `captured_at` lets the UI show staleness if every session goes quiet."""
    try:
        raw = json.loads(_RL.read_text())
    except (OSError, ValueError):
        return {"session": None, "weekly": None, "captured_at": None}
    rl = raw.get("rate_limits") or {}
    return {
        "session": _norm(rl.get("five_hour")),
        "weekly": _norm(rl.get("seven_day")),
        "captured_at": raw.get("captured_at"),
    }
