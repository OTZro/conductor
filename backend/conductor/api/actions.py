"""Card-action endpoints — the CORE ones: generic create/agent-hook/done/state/
links/group live here regardless of a card's origin. M8 step 3 moved the
source-specific handlers this router used to also carry out to their owning
plugin's own router: ``/hold`` + ``/awaiting`` + ``/resume`` (jira-only) →
``plugins.src_jira.router``; ``/slack-thread`` → ``plugins.src_slack.router``;
``/pr/{action}`` → ``plugins.src_github.router``. All four share this same
``/api/cards`` prefix (FastAPI mounts multiple routers under one prefix fine),
so no URL moved.

``card_body`` and ``refresh_prs`` stay here despite touching jira/github/slack:
``card_body`` is core's own bespoke-then-generic dispatch (jira/slack inline,
falling through to the plugin ``card_body`` spec surface for everyone else) —
the floor-for-new-kinds shape ``enrich.py``/``links.py`` already use.
``refresh_prs`` genuinely needs github AND jira AND enrich together behind one
``ok`` result; splitting it would need one plugin router importing another,
which M8 forbids, so it stays core glue instead."""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..auth import service as auth_service
from .. import enrich
from ..config import settings
from ..lanes import lane_for_card, stage_entry, stage_registry
from ..plugins.src_github import github as github_src  # via the source plugin (M5)
from ..plugins.src_jira import jira as jira_src  # via the source plugin (M5)
from ..bus import bus
from ..db import get_session
from ..models import Card, CardLink, LocalState, new_id, utcnow
from ..store import emit_lane_change, upsert_card
from .cards import serialize

log = logging.getLogger("conductor.actions")

router = APIRouter(prefix="/api/cards", tags=["actions"])


async def _load(session: AsyncSession, card_id: str) -> Card:
    card = await session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="card not found")
    return card


async def _serialize_current(
    session: AsyncSession, card: Card, *, registry: list[dict] | None = None
) -> dict:
    """``registry``: pass the snapshot this request already derived a lane against, so
    the body the client renders describes the same lane set as any lane_change event
    the request emitted. A handler that upserts first gets its snapshot by building one
    and handing it to BOTH ``upsert_card`` and this — the store derives lanes too, and
    without a shared snapshot the event and the response come from two different reads.

    Not passing it is correct only when the request derived no lane at all. Endpoints
    that do neither (a plain read-back) can omit it.

    Keyword-only, like every sibling that takes one (``lane_for_card``, ``serialize``,
    ``upsert_card``): a mis-slotted registry here is worse than a crash, because
    ``stage_entry`` would just iterate it and return a wrong lane."""
    ls = await session.get(LocalState, card.id)
    links = (
        await session.execute(select(CardLink).where(CardLink.card_id == card.id))
    ).scalars().all()
    return serialize(card, ls, list(links), registry=registry)


@router.post("")
async def create_manual(payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")
    # No snapshot to hoist here. `external_id=new_id()` makes this upsert ALWAYS a
    # create, and store.py derives lanes only on the not-created path, so nothing on the
    # write side would ever read one. The single lane this request derives is the
    # response's, inside serialize — the plain read-back case _serialize_current is
    # documented to take without an argument. Passing one anyway advertised a shared
    # snapshot that no code on this path exercises.
    card = await upsert_card(
        session,
        origin="manual",
        external_id=new_id(),
        title=title,
        summary=payload.get("summary") or "",
        url=payload.get("url"),
        # board tag: "" = the main work board; else the id of a local custom dashboard
        # (see ~/.conductor/dashboards.json). Keeps personal notes off the work board.
        cached_patch={"manual": {"open": True, "board": (payload.get("board") or "")}},
        link_text=payload.get("summary"),
    )
    return await _serialize_current(session, card)


@router.post("/{card_id}/agent")
async def agent_state(
    card_id: str, payload: dict, request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    """Called by a dashboard-launched Claude's hooks: working → AI Working,
    idle → cleared. "waiting" (→ Need Human) is IGNORED here even if an old
    hook file still sends it — the tmux pane poll is the single writer of
    waiting, because hook-time Stop/Notification can't tell a finished turn
    from one with a background task still running (the bounce bug)."""
    auth_service.require_ingest_token(request)
    card = await _load(session, card_id)
    state = (payload.get("state") or "").lower()
    if state == "waiting":
        return {"ok": True, "state": state, "ignored": "waiting is pane-poll-owned"}
    # working (user acted) or idle (ended) → claude is no longer blocked on you, so
    # any pending Notification message is stale; clear it in the same write.
    await upsert_card(
        session,
        origin=card.origin,
        external_id=card.external_id,
        cached_patch={
            "agent": {
                "active": state == "working",
                "running": state == "working",
                "waiting": False,
                "notification": None,
            }
        },
    )
    return {"ok": True, "state": state}


@router.post("/{card_id}/notify")
async def agent_notify(
    card_id: str, payload: dict, request: Request, session: AsyncSession = Depends(get_session)
) -> dict:
    """A dashboard-launched claude's Notification hook: claude is blocked on the
    human (permission / question) — the mobile-push event. We stash the raw
    `message` and show it as the card's amber banner; the next working/idle state
    clears it. The GENERIC idle nudge ("Claude is waiting for your input", fired for
    any ≥60s-idle prompt) is dropped: the pane poll's status line already says
    waiting, so the banner would duplicate it on every idle card — it's reserved
    for messages that name a concrete action (permission asks etc.)."""
    auth_service.require_ingest_token(request)
    card = await _load(session, card_id)
    msg = str(payload.get("message") or "").strip()
    if re.search(r"waiting for your input", msg, re.I):
        return {"ok": True, "ignored": "generic idle nudge — the pane poll already shows waiting"}
    if not msg and payload:
        # unknown payload shape → show it raw rather than nothing (also reveals the
        # real schema so the `message` key can be corrected if the guess was wrong)
        import json

        msg = json.dumps(payload, ensure_ascii=False)
    await upsert_card(
        session,
        origin=card.origin,
        external_id=card.external_id,
        cached_patch={"agent": {"notification": msg[:500] or None}},
    )
    return {"ok": True}


@router.post("/{card_id}/done")
async def set_done(
    card_id: str, payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    """Mark a manual or slack card done (→ Done lane) or reopen it. Jira/PR cards
    reach Done via their source (Jira status / PR merge)."""
    card = await _load(session, card_id)
    done = bool(payload.get("done", True))
    if card.origin == "manual":
        patch = {"manual": {"open": not done}}
    elif card.origin == "slack":
        patch = {"slack": {"done": done}}
    else:
        raise HTTPException(
            status_code=400, detail="only manual/slack cards can be marked done here"
        )
    # One snapshot for the lane_change event AND the response — but only when a
    # human-space lane is actually derived, which is the same condition store.py guards
    # its own build with. `done=True` always lands ball "none" (recompute_ball's
    # manual/slack terminals), so marking an ALREADY-done card done derives no lane on
    # either side and needs zero builds; an unconditional build here would reintroduce
    # one layer up exactly the waste that guard exists to avoid. `done=False` can land
    # human, so that direction keeps the snapshot.
    registry = stage_registry() if (not done or card.ball == "human") else None
    await upsert_card(
        session, origin=card.origin, external_id=card.external_id, cached_patch=patch,
        registry=registry,
    )
    return await _serialize_current(session, card, registry=registry)


@router.get("/{card_id}/body")
async def card_body(card_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    """The card's main content: Jira description (fetched on demand) or Slack
    message text. Other origins fall back to the card summary."""
    card = await _load(session, card_id)
    if card.origin == "jira":
        try:
            content = await jira_src.fetch_description(card.external_id)
        except Exception:  # noqa: BLE001
            content = ""
        # a subtask's info lives on its parent — fall back to the parent description
        pkey = (card.cached.get("jira") or {}).get("parent_key")
        if not content and pkey:
            try:
                pdesc = await jira_src.fetch_description(pkey)
                if pdesc:
                    content = f"↑ from parent {pkey}:\n\n{pdesc}"
            except Exception:  # noqa: BLE001
                pass
        return {"kind": "jira", "content": content}
    if card.origin == "slack":
        return {"kind": "slack", "content": (card.cached.get("slack") or {}).get("text") or ""}
    # plugin body providers — how a plugin SOURCE supplies its cards' main content
    # (what fetch_description is for jira). Enumerated off the kernel (spec_rows,
    # PLUGINS read-through) in registration order; first non-None wins; a broken
    # provider is skipped, and the summary fallback below still stands. Lazy (cycle).
    from ..plugins import runtime

    for _plugin_id, provider in runtime.spec_rows("card_body"):
        try:
            got = await provider({"origin": card.origin, "external_id": card.external_id})
        except Exception:  # noqa: BLE001 — a plugin must not break the body endpoint
            continue
        if isinstance(got, dict) and got.get("content") is not None:
            return {"kind": got.get("kind") or card.origin, "content": got["content"]}
    return {"kind": card.origin, "content": card.summary or ""}


@router.post("/{card_id}/refresh-prs")
async def refresh_prs(card_id: str) -> dict:
    """On-demand link refresh for this card (bypasses the poll throttles) — called when
    a detail opens so PR CI/review AND linked-jira hover data are current. The upserts
    publish card.upsert over the WS, so the UI updates itself."""
    ok = await github_src.refresh_card_prs(card_id)
    try:
        await jira_src.refresh_card_jiras(card_id)
    except Exception:  # noqa: BLE001 — jira hover data is best-effort on open
        pass
    try:
        await enrich.refresh_card_linkmeta(card_id)
    except Exception:  # noqa: BLE001 — plugin link meta is best-effort on open
        pass
    return {"ok": ok}


@router.patch("/{card_id}/state")
async def patch_state(
    card_id: str, payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    card = await _load(session, card_id)
    ls = await session.get(LocalState, card.id)
    if ls is None:
        ls = LocalState(card_id=card.id)
        session.add(ls)
    if "read" in payload:
        ls.read = bool(payload["read"])
    if "dismissed" in payload:
        ls.dismissed = bool(payload["dismissed"])
    if "pinned" in payload:
        ls.pinned = bool(payload["pinned"])
    if "note" in payload:
        ls.note = payload["note"]
    if payload.get("snooze_hours"):
        ls.snoozed_until = utcnow() + timedelta(hours=float(payload["snooze_hours"]))
    if payload.get("unsnooze"):
        ls.snoozed_until = None
    old_lane = None
    registry: list[dict] | None = None
    if "stage" in payload:
        # manual board-stage override: a registered HUMAN-space stage key, or null to
        # return the card to its derived lane. Validated against the live registry so a
        # typo'd/deleted stage 400s here instead of silently no-oping on the board.
        #
        # ONE snapshot spans the validation, both lane derivations and the commit between
        # them — same reason as store.py's transition test. Read separately (as this used
        # to), a lanes.json save mid-request could validate the stage against one lane set
        # and judge the transition against another, firing a phantom lane change.
        registry = stage_registry()
        stage = payload["stage"]
        if stage is not None:
            stage = str(stage)
            entry = stage_entry(stage, registry=registry)
            if not entry or entry.get("within") != "human":
                raise HTTPException(status_code=400, detail=f"unknown or non-manual stage: {stage}")
        old_lane = lane_for_card(
            card.cached or {}, card.ball, settings.jira_hold_label, ls.manual_stage,
            registry=registry,
        )
        ls.manual_stage = stage
    await session.commit()
    bus.publish({"type": "card.upsert", "id": card.id})
    if old_lane is not None:
        new_lane = lane_for_card(
            card.cached or {}, card.ball, settings.jira_hold_label, ls.manual_stage,
            registry=registry,
        )
        if new_lane != old_lane:
            emit_lane_change(card.id, card.origin, card.external_id, old_lane, new_lane, card.ball)
    # same snapshot as the derivations above (None when no stage was touched, i.e. when
    # nothing in this request derived a lane) so the body the client renders and the
    # event plugins receive describe the same lane set
    return await _serialize_current(session, card, registry=registry)


@router.post("/{card_id}/links")
async def add_link(
    card_id: str, payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    card = await _load(session, card_id)
    url = payload.get("url") or payload.get("ref")
    if not url:
        raise HTTPException(status_code=400, detail="url required")
    ref = payload.get("ref") or url
    kind = payload.get("kind") or "url"
    existing = (
        await session.execute(
            select(CardLink).where(
                CardLink.card_id == card.id, CardLink.kind == kind, CardLink.ref == ref
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        session.add(
            CardLink(
                card_id=card.id,
                kind=kind,
                ref=ref,
                url=url,
                title=payload.get("title"),
                auto=False,
            )
        )
        await session.commit()
        bus.publish({"type": "card.upsert", "id": card.id})
    return await _serialize_current(session, card)


@router.post("/{card_id}/group")
async def group_card(
    card_id: str, payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    """Group `card_id` UNDER a representative card (`primary_id`) so several jobs for one
    task manage as one: the member is hidden from the main board and listed under the
    representative. `primary_id` null/empty ungroups it. Flat only — grouping under a card
    that is itself a member resolves to that member's own representative."""
    card = await session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="card not found")
    primary_id = (payload.get("primary_id") or "").strip() or None
    if primary_id == card_id:
        raise HTTPException(status_code=400, detail="can't group a card under itself")
    if primary_id:
        primary = await session.get(Card, primary_id)
        if not primary:
            raise HTTPException(status_code=404, detail="representative card not found")
        primary_id = ((primary.cached or {}).get("group")) or primary_id  # no nesting
    cached = dict(card.cached or {})
    if primary_id:
        cached["group"] = primary_id
    else:
        cached.pop("group", None)
    card.cached = cached
    card.updated_at = utcnow()
    await session.commit()
    bus.publish({"type": "card.upsert", "id": card.id})
    return {"ok": True, "group": primary_id}
