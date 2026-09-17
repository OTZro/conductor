"""Per-poller health registry, fed by main._run_poller (the single choke point
every background job goes through). In-memory on purpose: it describes THIS
process's loops; a restart resetting it is correct.

`snapshot()` powers GET /api/status and the frontend health dot — the answer to
"is the board actually fresh, or has a source been silently failing?" (a slack
token expiry or a dead jira loop previously showed up as nothing at all)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

log = logging.getLogger("conductor")

# poller name -> {last_success: epoch|None, last_error: epoch|None, error: str|None}
_pollers: dict[str, dict[str, Any]] = {}


def record_ok(name: str) -> None:
    entry = _pollers.setdefault(name, {"last_success": None, "last_error": None, "error": None})
    entry["last_success"] = time.time()
    entry["error"] = None


def record_error(name: str, exc: BaseException) -> None:
    entry = _pollers.setdefault(name, {"last_success": None, "last_error": None, "error": None})
    entry["last_error"] = time.time()
    entry["error"] = f"{type(exc).__name__}: {exc}"[:300]


def snapshot() -> dict[str, Any]:
    now = time.time()
    out: dict[str, Any] = {}
    for name, e in sorted(_pollers.items()):
        out[name] = {
            "age_s": round(now - e["last_success"], 1) if e["last_success"] else None,
            "error": e["error"],
            "error_age_s": round(now - e["last_error"], 1) if e["last_error"] else None,
        }
    return out


async def run_step(name: str, fn: Callable[[], Awaitable[Any]]) -> None:
    """Run one named step of a source loop, recording its freshness under NAME.

    Extracted from main._run_poller so source PLUGINS (plugins/src_*) can keep
    reporting under the exact legacy poller names ("jira", "github", "slack", …)
    that /api/status consumers and the FE health card key on, while the loop itself
    rides the kernel's PollSpec framework. Same contract as
    _run_poller: errors are recorded + logged, never raised (a failing step must
    not abort the rest of a composite sequence)."""
    try:
        await fn()
        record_ok(name)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        record_error(name, exc)
        log.warning("[poll:%s] %s", name, exc)
