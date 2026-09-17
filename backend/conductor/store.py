from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from . import notify
from .bus import bus
from .config import settings
from .db import session_maker
from .lanes import lane_for_card, recompute_ball, stage_registry
from .links import discover_links
from .models import Card, CardLink, LocalState, TerminalSession, utcnow

log = logging.getLogger("conductor.store")

# Serialize upserts per card. Five loops (tailer 2s, agent 5s, jira 30s, aux 30s,
# dates 300s) all read-modify-write the same `cached` dict; without this, two
# concurrent writers to different subkeys lose one of the updates (last commit
# wins over the whole JSON column). Single-process, so an asyncio.Lock suffices —
# no SELECT FOR UPDATE needed. ~1 lock per live card; never pruned (bounded by
# the board size, a few hundred entries at most).
_card_locks: dict[tuple[str, str], asyncio.Lock] = {}


def _card_lock(origin: str, external_id: str) -> asyncio.Lock:
    return _card_locks.setdefault((origin, external_id), asyncio.Lock())


# strong refs to in-flight lane-change callback tasks — the event loop keeps only a
# weak ref to a task, so a discarded create_task return could be GC'd before it runs,
# silently dropping the very plugin trigger this exists to deliver. Self-clears on
# completion.
_lane_change_tasks: set[asyncio.Task] = set()


def emit_lane_change(
    card_id: str, origin: str, external_id: str, old_lane: str, new_lane: str, ball: str
) -> None:
    """Notify every plugin's LaneChangeSpec of a REAL lane transition — the trigger
    surface for stage machinery (e.g. an orchestrator spawning the next agent when a
    card lands in its stage). Fire-and-forget with a per-callback timeout; a broken
    plugin logs and is dropped, never blocks the upsert path. Only actual transitions
    fire (callers compare first), which is what bounds claim-writing callbacks."""
    from .plugins import runtime  # lazy — plugins import this module

    ctx = {
        "card_id": card_id, "origin": origin, "external_id": external_id,
        "old_lane": old_lane, "new_lane": new_lane, "ball": ball,
    }
    for plugin_id, spec in runtime.spec_rows("lane_changes"):

        async def _call(cb=spec.callback, pid=plugin_id):
            try:
                await asyncio.wait_for(cb(dict(ctx)), 5.0)
            except Exception as exc:  # noqa: BLE001 — a plugin must not break the store
                log.warning("[lanes] plugin %s lane-change callback failed: %s", pid, exc)

        task = asyncio.create_task(_call())
        _lane_change_tasks.add(task)
        task.add_done_callback(_lane_change_tasks.discard)


async def touch_card(session: AsyncSession, card_id: str) -> None:
    """Bump a card's ``updated_at`` and publish a ``card.upsert`` — the generic
    live-refresh primitive. A plugin that changes a card's out-of-band state (e.g.
    stashes a file in its own store) calls this so an open card drawer re-fetches
    its widgets, and the board re-sorts. Core owns no plugin-specific shape here;
    the bump + publish is all the framework needs to surface a plugin's change.
    Runs under the per-card lock so it can't clobber a concurrent upsert's
    ``updated_at`` write."""
    card = await session.get(Card, card_id)
    if card is None:
        return
    async with _card_lock(card.origin, card.external_id):
        await session.refresh(card)
        card.updated_at = utcnow()
        await session.commit()
        origin, external_id = card.origin, card.external_id
    bus.publish({"type": "card.upsert", "id": card_id, "origin": origin, "external_id": external_id})


async def upsert_card(
    session: AsyncSession,
    *,
    origin: str,
    external_id: str,
    title: str | None = None,
    summary: str | None = None,
    url: str | None = None,
    driver: str | None = None,
    cached_patch: dict[str, Any] | None = None,
    cached_replace: dict[str, Any] | None = None,
    link_text: str | None = None,
    extra_links: list[dict] | None = None,
    create: bool = True,
    registry: list[dict] | None = None,
) -> Card | None:
    """Create or update a card keyed by (origin, external_id), deep-merging
    ``cached_patch`` into the per-source ``cached`` sub-dicts, recomputing the
    ball, ensuring a LocalState row, and merging discovered links.

    ``cached_replace`` OVERWRITES its keys wholesale instead of sub-merging — for
    snapshot-shaped values (e.g. a ticket's current PR set) where a merge would
    accumulate stale entries forever.

    ``registry`` is the caller's stage_registry() snapshot, for a caller that will
    ALSO derive a lane itself (an endpoint that upserts then serializes its response).
    Without it this builds its own, so the lane_change event plugins receive and the
    lane the endpoint then renders come from two different reads — the divergence a
    snapshot exists to prevent, at the endpoint/store seam. Omit it and one is built
    here as needed.

    The whole read-modify-write (including the commit) runs under the per-card
    lock so concurrent pollers can't clobber each other's `cached` subkeys."""
    async with _card_lock(origin, external_id):
        return await _upsert_card_locked(
            session,
            origin=origin, external_id=external_id, title=title, summary=summary,
            url=url, driver=driver, cached_patch=cached_patch,
            cached_replace=cached_replace, link_text=link_text,
            extra_links=extra_links, create=create, registry=registry,
        )


async def _upsert_card_locked(
    session: AsyncSession,
    *,
    origin: str,
    external_id: str,
    title: str | None = None,
    summary: str | None = None,
    url: str | None = None,
    driver: str | None = None,
    cached_patch: dict[str, Any] | None = None,
    cached_replace: dict[str, Any] | None = None,
    link_text: str | None = None,
    extra_links: list[dict] | None = None,
    create: bool = True,
    registry: list[dict] | None = None,
) -> Card | None:
    res = await session.execute(
        select(Card).where(Card.origin == origin, Card.external_id == external_id)
    )
    card = res.scalar_one_or_none()
    if card is not None:
        # the caller's session may have loaded this card BEFORE we acquired the lock
        # (identity map returns the stale instance) — refresh to the latest committed
        # state so the read-modify-write below starts from truth.
        await session.refresh(card)
    now = utcnow()
    created = card is None
    if card is None:
        if not create:
            return None  # annotate-only (e.g. an external event tailer): skip unknown cards
        card = Card(origin=origin, external_id=external_id, created_at=now)
        session.add(card)

    # `pr_search_ts` / `jira_link_ts` / `linkmeta_ts` are per-poll bookkeeping; exclude from the
    # change check so enrichment doesn't bump updated_at and churn the board order.
    prev_cached = {k: v for k, v in (card.cached or {}).items() if k not in ("pr_search_ts", "jira_link_ts", "linkmeta_ts")}
    prev = (
        prev_cached, card.title, card.summary, card.url,
        card.driver, card.ball, card.agent_state,
    )
    # full pre-mutation snapshot + the pre-existing manual stage — inputs for the
    # old-lane computation that decides whether to emit a lane-change event below.
    before_cached = dict(card.cached or {})
    prev_ls = None if created else await session.get(LocalState, card.id)
    prev_manual = prev_ls.manual_stage if prev_ls else None
    cached = dict(card.cached or {})
    if cached_patch:
        for key, val in cached_patch.items():
            if isinstance(val, dict):
                sub = dict(cached.get(key) or {})
                sub.update(val)
                cached[key] = sub
            else:
                cached[key] = val
    if cached_replace:
        # overwrite wholesale — snapshot-shaped keys (e.g. `prs`) must not accumulate
        # stale sub-entries the way a sub-merge would.
        for key, val in cached_replace.items():
            cached[key] = val
    card.cached = cached  # reassign so SQLAlchemy tracks the change

    if title is not None:
        card.title = title
    if summary is not None:
        card.summary = summary
    if url is not None:
        card.url = url
    if driver is not None:
        card.driver = driver

    prev_ball = card.ball
    ball, agent_state = recompute_ball(
        cached, hold_label=settings.jira_hold_label, origin=origin
    )
    card.ball = ball
    card.agent_state = agent_state

    now_cached = {k: v for k, v in cached.items() if k not in ("pr_search_ts", "jira_link_ts", "linkmeta_ts")}
    now_state = (now_cached, card.title, card.summary, card.url, card.driver, ball, agent_state)
    changed = created or prev != now_state
    if changed:
        card.updated_at = now  # only bump on real change → stable board order
    card.last_seen_at = now
    await session.flush()  # assign card.id

    ls = await session.get(LocalState, card.id)
    if ls is None:
        ls = LocalState(card_id=card.id)
        session.add(ls)
    elif ball == "none" and ls.manual_stage:
        # finished → drop the manual stage override; a later reopen derives its lane
        # fresh instead of silently resurrecting into an old Pending-style column.
        ls.manual_stage = None

    found: list[dict] = []
    if link_text:
        found += discover_links(link_text, settings.jira_base_url)
    if extra_links:
        found += extra_links
    # a github PR card linking to its own PR is redundant; a jira card SHOWING its
    # own jira link is wanted (the board chip), so only filter the github self-link.
    found = [
        link
        for link in found
        if not (origin == "github" and link["kind"] == "pr" and link["ref"] == external_id)
    ]
    links_added = await _merge_links(session, card.id, found) if found else False

    await session.commit()
    if changed or links_added:
        bus.publish(
            {"type": "card.upsert", "id": card.id, "origin": origin, "external_id": external_id}
        )
    if not created:
        # lane-change event for plugins — computed from the pre/post snapshots so it
        # fires only on REAL transitions (incl. those caused by the auto-clear above).
        # one snapshot for both derivations: comparing them against the SAME lane set is
        # what makes the transition test meaningful — a lanes.json save landing between
        # the two calls could otherwise manufacture a phantom lane change and fire
        # plugin callbacks off it.
        #
        # Built ONLY when a human-ball lane is actually derived. lane_for_card returns
        # for ball ai/none before it reads the registry, so an unconditional build here
        # made an ai/none upsert do strictly MORE work than before this branch existed
        # (0 builds -> 1) — a regression on a board full of AI Working / Done cards,
        # from a change whose whole point is the opposite.
        if registry is None and "human" in (prev_ball, ball):
            registry = stage_registry()
        old_lane = lane_for_card(
            before_cached, prev_ball, settings.jira_hold_label, prev_manual, registry=registry
        )
        new_lane = lane_for_card(
            cached, ball, settings.jira_hold_label, ls.manual_stage, registry=registry
        )
        if old_lane != new_lane:
            emit_lane_change(card.id, origin, external_id, old_lane, new_lane, ball)
    if ball == "human" and prev_ball != "human":
        asyncio.create_task(
            notify.need_human(
                card.title or external_id, f"{origin} · {agent_state or ''}", external_id
            )
        )
    if ball == "none" and prev_ball != "none":
        # reached Done → free its Conductor tmux/claude (resume from the UI still works)
        from .actions import terminal as term_actions

        asyncio.create_task(term_actions.kill_card_sessions(card.id))
    return card


async def _merge_links(session: AsyncSession, card_id: str, links: list[dict]) -> bool:
    existing = (
        await session.execute(select(CardLink).where(CardLink.card_id == card_id))
    ).scalars().all()
    seen = {(link.kind, link.ref) for link in existing}
    added = False
    for link in links:
        pair = (link["kind"], link["ref"])
        if pair in seen:
            continue
        session.add(
            CardLink(
                card_id=card_id,
                kind=link["kind"],
                ref=link["ref"],
                url=link["url"],
                title=link.get("title"),
                auto=bool(link.get("auto", True)),
            )
        )
        seen.add(pair)
        added = True
    return added


async def delete_card_cascade(session: AsyncSession, card: Card) -> None:
    """Delete a card and its dependents. The ONE copy of the cascade — it used to
    be hand-copied in every prune site, which is how new dependent tables get
    silently missed."""
    for model in (CardLink, LocalState, TerminalSession):
        await session.execute(delete(model).where(model.card_id == card.id))
    await session.delete(card)


async def prune_source(
    origin: str,
    keep: set[str],
    keep_if=None,
) -> int:
    """Delete `origin`'s cards whose external_id is no longer in `keep` (the ids the
    source's poll just surfaced). `keep_if(card, local_state)` is the per-source
    escape hatch — e.g. slack keeps dismissed/done resolutions and un-actioned
    mentions. Replaces the per-source hand-rolled prune loops."""
    async with session_maker() as session:
        cards = (
            await session.execute(select(Card).where(Card.origin == origin))
        ).scalars().all()
        states: dict[str, LocalState] = {}
        if keep_if:
            states = {
                s.card_id: s
                for s in (await session.execute(select(LocalState))).scalars().all()
            }
        removed = 0
        for c in cards:
            if c.external_id in keep:
                continue
            if keep_if and keep_if(c, states.get(c.id)):
                continue
            await delete_card_cascade(session, c)
            removed += 1
        if removed:
            await session.commit()
            bus.publish({"type": "card.pruned", "count": removed})
        return removed


async def prune_done(max_age_days: int) -> int:
    """Age cards out of the Done lane (ball == 'none'). Jira is handled at the board
    query (resolved filter); here we drop slack done by message age and other origins
    by updated_at, so Done doesn't accumulate forever."""
    cutoff = utcnow() - timedelta(days=max_age_days)
    slack_cutoff = time.time() - max_age_days * 86400
    async with session_maker() as session:
        cards = (
            await session.execute(select(Card).where(Card.ball == "none"))
        ).scalars().all()
        removed = 0
        for c in cards:
            if c.origin == "jira":
                continue  # board JQL already drops long-resolved tickets
            if c.origin == "slack":
                ts = ((c.cached or {}).get("slack") or {}).get("ts")
                try:
                    old = float(ts) < slack_cutoff
                except (TypeError, ValueError):
                    old = False
            else:
                old = bool(c.updated_at and c.updated_at < cutoff)
            if not old:
                continue
            await delete_card_cascade(session, c)
            removed += 1
        if removed:
            await session.commit()
            bus.publish({"type": "done.pruned", "count": removed})
        return removed
