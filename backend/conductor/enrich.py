"""Generic link-kind enrichment — the shared loop behind plugin ``LinkEnricherSpec``s.

The built-in PR (cached.prs) and jira (cached.jiras) enrichers keep their bespoke
richness; this is the FLOOR for new kinds: a plugin declares (kind, fetch) and core
does what those two loops do — find the cards carrying such links, throttle per card,
call fetch per ref (bounded), snapshot into ``cached.linkmeta[kind][ref]`` — so the FE
can render a generic hover for any link kind with no per-kind code anywhere.

Storage: ``cached.linkmeta`` is a per-card snapshot ({kind: {ref: meta}}); the
throttle timestamp ``linkmeta_ts`` is excluded from the updated_at change check (like
``pr_search_ts``) so bookkeeping never churns board order."""

from __future__ import annotations

import asyncio
import datetime
import logging

from sqlalchemy import select

from . import store
from .db import session_maker
from .models import Card, CardLink

log = logging.getLogger("conductor.enrich")

_FETCH_TIMEOUT_S = 10.0


def _specs() -> dict:
    """kind -> LinkEnricherSpec across registered plugins (first per kind wins).
    Imported lazily — plugins import this module's consumers."""
    from .plugins import runtime

    out: dict = {}
    for _plugin_id, spec in runtime.spec_rows("link_enrichers"):
        if spec.kind in out:
            log.warning("[enrich] duplicate enricher for kind %r — first wins", spec.kind)
            continue
        out[spec.kind] = spec
    return out


def _stale(card: Card, now: datetime.datetime, max_age_s: int = 600) -> bool:
    ts = (card.cached or {}).get("linkmeta_ts")
    if not ts:
        return True
    try:
        return (now - datetime.datetime.fromisoformat(ts)).total_seconds() > max_age_s
    except (TypeError, ValueError):
        return True


async def _enrich_card(card: Card, specs: dict, now: datetime.datetime) -> bool:
    async with session_maker() as session:
        links = (
            await session.execute(
                select(CardLink).where(
                    CardLink.card_id == card.id, CardLink.kind.in_(list(specs))
                )
            )
        ).scalars().all()
    refs = [link for link in links if link.ref]

    async def _fetch(link: CardLink) -> dict | None:
        try:
            return await asyncio.wait_for(specs[link.kind].fetch(link.ref), _FETCH_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 — an unreachable ref just stays a plain chip
            log.debug("[enrich] %s %s failed: %s", link.kind, link.ref, exc)
            return None

    # concurrent, not serial — a card with several links must cost one timeout, not N×
    fetched = await asyncio.gather(*(_fetch(link) for link in refs))
    prev = (card.cached or {}).get("linkmeta") or {}
    meta: dict[str, dict] = {}
    for link, got in zip(refs, fetched):
        if isinstance(got, dict):
            meta.setdefault(link.kind, {})[link.ref] = got
        else:
            # a transient fetch failure must not wipe previously-known data for a ref
            # that's still linked — fall back to the last snapshot's value, if any
            stale = (prev.get(link.kind) or {}).get(link.ref)
            if stale is not None:
                meta.setdefault(link.kind, {})[link.ref] = stale
    async with session_maker() as session:
        await store.upsert_card(
            session,
            origin=card.origin,
            external_id=card.external_id,
            cached_patch={"linkmeta_ts": now.isoformat()},
            # snapshot semantics — track the current link set, don't accumulate stale refs
            cached_replace={"linkmeta": meta},
            create=False,
        )
    return bool(meta)


async def enrich_plugin_links(limit: int = 8) -> int:
    """Poll: run every plugin's LinkEnricherSpec over the cards that carry its link
    kind, throttled per card — the plugin never writes a loop."""
    specs = _specs()
    if not specs:
        return 0
    now = datetime.datetime.now(datetime.timezone.utc)
    async with session_maker() as session:
        card_ids = set(
            (
                await session.execute(
                    select(CardLink.card_id).where(CardLink.kind.in_(list(specs)))
                )
            ).scalars().all()
        )
        cards = (
            (await session.execute(select(Card).where(Card.id.in_(card_ids)))).scalars().all()
            if card_ids
            else []
        )
    count = 0
    for card in [c for c in cards if _stale(c, now)][:limit]:
        if await _enrich_card(card, specs, now):
            count += 1
    return count


async def refresh_card_linkmeta(card_id: str) -> bool:
    """On-demand (drawer open): refresh ONE card's plugin link meta now."""
    specs = _specs()
    if not specs:
        return False
    async with session_maker() as session:
        card = await session.get(Card, card_id)
    if not card:
        return False
    return await _enrich_card(card, specs, datetime.datetime.now(datetime.timezone.utc))
