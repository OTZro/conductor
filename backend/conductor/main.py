from __future__ import annotations

import asyncio
import logging
import math
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from . import __version__, enrich, status, store
from .actions import terminal as term_actions
from .api import actions, auth, board, cards, dashboards, plugins, terminals, termkeys, termproxy, themes, usage, ws
from .plugins import PLUGIN_ROUTERS
from .plugins import runtime as plugin_runtime
from .auth.middleware import AuthMiddleware
from .config import settings, warn_unknown_keys
from .db import init_db, session_maker
from .logging_setup import setup_logging

setup_logging()
log = logging.getLogger("conductor")

_tasks: list[asyncio.Task] = []

# A fresh random id per PROCESS (not per boot-time config, not a git sha — those
# reflect DISK state, which can change before a restart actually takes effect).
# /api/health exposes it so a caller polling through a restart (the marketplace
# apply flow) can tell "a new process answered" apart from "the same old process
# is still here, just healthy" — a health-only poll can't distinguish those, and a
# heuristic based on an observed down-period can miss a restart fast enough to land
# entirely between two polls.
_PROCESS_GENERATION = uuid.uuid4().hex


async def _run_poller(name: str, fn) -> None:
    try:
        await fn()
        status.record_ok(name)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        status.record_error(name, exc)
        log.warning("[poll:%s] %s", name, exc)


# The SOURCE loops that used to live here (_tail_loop, _jira_loop, and the source
# steps of _aux_loop/_dates_loop) are now declared by the source plugins
# (plugins/src_jira, src_github, src_slack) as PollSpecs and ride
# _plugin_poll_loop like any other plugin poll — main.py names none of them by
# import, only the generic discovery/PLUGIN_ROUTERS surface (M8: there is no
# longer a conductor.sources dotted path at all — the plugin package is the
# sole implementation). Their steps still record under the legacy /api/status
# names (see status.run_step); only the CORE steps of those loops remain below.


async def _enrich_loop() -> None:
    """Plugin link-kind enrichment (LinkEnricherSpec) — core machinery, not a
    source: it fans out to whatever enrichers plugins registered."""
    await asyncio.sleep(5)
    while True:
        await _run_poller("link-enrich", enrich.enrich_plugin_links)
        await asyncio.sleep(settings.poll_interval_s)


async def _housekeeping_loop() -> None:
    """Slow core sweeps: age cards out of Done, reap orphaned tmux sessions.
    done-prune is age-gated (done_prune_days), so it needs no ordering against
    the sources' own slow sweeps (github-pr-refresh flips merged PRs to Done;
    they only become prune-eligible days later)."""
    await asyncio.sleep(8)
    while True:
        await _run_poller("done-prune", lambda: store.prune_done(settings.done_prune_days))
        await _run_poller("tmux-reap", term_actions.reap_orphan_tmux)
        await asyncio.sleep(settings.dates_interval_s)


async def _agent_loop() -> None:
    """Ground-truth agent state from live claude panes — the authority that corrects
    stale hook state (a missed Stop/SessionEnd, or a closed modal) so a running job
    isn't stranded in Need Human."""
    await asyncio.sleep(6)
    while True:
        await _run_poller("agent-state", term_actions.poll_agent_states)
        await asyncio.sleep(settings.agent_poll_s)


async def _plugin_poll_loop(name: str, fn, interval: float, *, report_status: bool = True) -> None:
    """One plugin-declared PollSpec, riding the same _run_poller framework as the
    built-in sources: errors recorded + logged (never fatal), freshness in /api/status
    under "<plugin-id>.<name>" so the health dot judges the plugin like any source.
    ``report_status=False`` (the source plugins' composite loops) skips that row —
    their steps already report under the legacy names via status.run_step, and the
    aggregate would be always-green noise."""
    await asyncio.sleep(10)  # let the core seed (jira/github/agent) settle first
    while True:
        if report_status:
            await _run_poller(name, fn)
        else:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("[poll:%s] %s", name, exc)
        await asyncio.sleep(interval)


_PLUGIN_POLLS: list[tuple[str, object, float]] | None = None
# full names of polls that asked NOT to report (PollSpec.report_status=False);
# rebuilt alongside _PLUGIN_POLLS so a test's cache reset refreshes both.
_SILENT_POLLS: set[str] = set()


def _plugin_polls() -> list[tuple[str, object, float]]:
    """(status-name, fn, interval) for every plugin-declared PollSpec — validated:
    a duplicate full name would collapse two pollers onto one /api/status entry, and
    a non-positive/non-finite interval would make the loop spin or hang. Bad specs
    are logged and skipped (a broken plugin must not take the poller framework down).
    Cached after the first call — registrations are fixed after startup discovery, and
    system_status() calls this on every /api/status request; recomputing (and
    re-warning on the same bad spec) per request is wasted work and log spam."""
    global _PLUGIN_POLLS
    if _PLUGIN_POLLS is not None:
        return _PLUGIN_POLLS
    out: list[tuple[str, object, float]] = []
    seen: set[str] = set()
    silent: set[str] = set()
    for pid, spec in plugin_runtime.spec_rows("polls"):
        full = f"{pid}.{spec.name}"
        if full in seen:
            log.warning("[plugins] duplicate poll name %r — skipped", full)
            continue
        if not (
            isinstance(spec.interval, (int, float))
            and math.isfinite(spec.interval)
            and spec.interval > 0
        ):
            log.warning("[plugins] poll %r has invalid interval %r — skipped", full, spec.interval)
            continue
        seen.add(full)
        out.append((full, spec.fn, float(spec.interval)))
        if not getattr(spec, "report_status", True):
            silent.add(full)
    _PLUGIN_POLLS = out
    _SILENT_POLLS.clear()
    _SILENT_POLLS.update(silent)
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    warn_unknown_keys()  # surface silently-ignored CONDUCTOR_* typos in the log
    await init_db()
    # a prior run's ttyd processes are orphans now (their _SESSIONS handles died with
    # that process) — reap them so their ports free up instead of leaking each restart.
    await _run_poller("ttyd-reap", term_actions.reap_orphan_ttyd)
    _tasks.append(asyncio.create_task(_agent_loop()))
    _tasks.append(asyncio.create_task(_enrich_loop()))
    _tasks.append(asyncio.create_task(_housekeeping_loop()))
    # every periodic job besides the core loops above is a kernel-served PollSpec —
    # the four data sources included (plugins/src_*) — so lifespan just iterates them
    for _pname, _pfn, _pinterval in _plugin_polls():
        _tasks.append(asyncio.create_task(_plugin_poll_loop(
            _pname, _pfn, _pinterval, report_status=_pname not in _SILENT_POLLS)))
    try:
        yield
    finally:
        for t in _tasks:
            t.cancel()
        for t in _tasks:
            try:
                await t
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(title="Conductor", version=__version__, lifespan=lifespan)
# Starlette add_middleware PREPENDS (last added = outermost). AuthMiddleware is added
# before CORS so the final order is CORS → Auth → routes: middleware 401/4401
# responses still get CORS headers on the Vite dev origin.
app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router)
app.include_router(board.router)
app.include_router(cards.router)
app.include_router(actions.router)
app.include_router(terminals.router)
app.include_router(plugins.router)
app.include_router(usage.router)
app.include_router(dashboards.router)
app.include_router(termkeys.router)
app.include_router(themes.router)
# self-contained plugins may bring their own backend router (discovered, incl. gitignored
# local/) — jira's /api/jira + /api/pins + its /api/cards/* actions, github's PR action,
# and slack's thread endpoint all ride this (M8 step 3; formerly api.jira / api.pins /
# api.ingest and three api.actions handlers).
for _plugin_router in PLUGIN_ROUTERS:
    app.include_router(_plugin_router)
app.include_router(termproxy.router)
app.include_router(ws.router)


@app.get("/api/status")
async def system_status() -> dict:
    """Per-poller freshness for the frontend health dot. `interval_s` is each
    poller's expected cadence so the client can judge staleness as a multiple of
    it (age > 3× interval → degraded) instead of hardcoding thresholds."""
    intervals = {
        "agent-state": settings.agent_poll_s,
        # the pane captures inside that poll, reported separately: _run_poller's
        # record_ok covers "agent-state" the moment poll_agent_states returns, so a
        # capture that raised and was contained can only surface under its own name.
        "agent-state-panes": settings.agent_poll_s,
        "jira": settings.poll_interval_s,
        "github": settings.poll_interval_s,
        "github-pr-enrich": settings.poll_interval_s,
        "github-pr-enrich-manual": settings.poll_interval_s,
        "jira-links": settings.poll_interval_s,
        "link-enrich": settings.poll_interval_s,
        "slack": settings.poll_interval_s,
        "github-pr-refresh": settings.dates_interval_s,
        "done-prune": settings.dates_interval_s,
        "tmux-reap": settings.dates_interval_s,
        # ttyd-reap is deliberately absent: it runs ONCE at startup (lifespan), so
        # advertising a cadence would make the FE flag it stale forever after boot.
        # interval_s=null marks it one-shot; the FE then reports only real errors.
        "github-pr-brief": settings.dates_interval_s,
        "jira-dates": settings.dates_interval_s,
    }
    for _pname, _pfn, _pinterval in _plugin_polls():
        if _pname not in _SILENT_POLLS:  # silent composites never appear as pollers
            intervals[_pname] = _pinterval
    pollers = status.snapshot()
    for name, meta in pollers.items():
        meta["interval_s"] = intervals.get(name)
    return {"pollers": pollers}


@app.get("/api/health")
async def health() -> JSONResponse:
    """Liveness + DB reachability. The 2026-07-03 postgres outage returned 200 here
    while every board query failed — health must touch the DB to mean anything.
    Auth-exempt (see auth/middleware.py) so unauthenticated probes work."""
    db_ok = True
    try:
        async with asyncio.timeout(3):
            async with session_maker() as s:
                await s.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001
        db_ok = False
    body = {
        "ok": db_ok,
        "db": "up" if db_ok else "down",
        "version": __version__,
        "slack_enabled": bool(settings.slack_token),
        "generation": _PROCESS_GENERATION,
    }
    return JSONResponse(body, status_code=200 if db_ok else 503)


# Serve the built frontend when present (production). In dev, Vite runs on :5173.
_dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=str(_dist), html=True), name="frontend")
