from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import APIRouter

router = APIRouter(prefix="/api", tags=["usage"])

# The statusline persists Claude Code's rate-limit snapshot here on every active
# session render (see statusline-command.sh). Account-wide, so this one file — kept
# fresh by the always-running conductor sessions — is authoritative.
_RL = Path.home() / ".claude" / "rate-limits.json"


def _norm(x: object, *, now: float) -> dict | None:
    """One window, with an EXPIRED snapshot reported as unknown rather than stale.

    The snapshot is push-based: the file only changes when some Claude Code session
    renders its statusline. Once a window rolls over, nothing rewrites it until the
    next render — for the 7-day window that can be hours. Serving the pre-reset
    percentage through that gap is how the header sat at "WEEK 88%" long after the
    reset. ``resets_at`` in the past is proof the number is obsolete, so the
    percentage goes None and ``expired`` says why; the reset time is kept so the UI
    can still say when it rolled.
    """
    if not isinstance(x, dict):
        return None
    resets_at = x.get("resets_at")
    expired = isinstance(resets_at, (int, float)) and not isinstance(resets_at, bool) and resets_at <= now
    return {
        "used_percentage": None if expired else x.get("used_percentage"),
        "resets_at": resets_at,
        "expired": expired,
    }


@router.get("/usage")
async def usage() -> dict:
    """Claude Code session (5h) + weekly (7d) usage with reset times. null fields
    until a session has reported (fresh install / no claude.ai subscription), and
    again once a window's reset time has passed but no session has reported the new
    window yet (``expired``). `captured_at` lets the UI show staleness if every
    session goes quiet."""
    try:
        raw = json.loads(_RL.read_text())
    except (OSError, ValueError):
        return {"session": None, "weekly": None, "captured_at": None}
    rl = raw.get("rate_limits") or {}
    now = time.time()
    return {
        "session": _norm(rl.get("five_hour"), now=now),
        "weekly": _norm(rl.get("seven_day"), now=now),
        "captured_at": raw.get("captured_at"),
    }
