from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ..config import settings
from ..db import get_session
from ..lanes import lane_for_card, stage_registry
from ..models import Card, CardLink, LocalState

router = APIRouter(prefix="/api/cards", tags=["cards"])


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def serialize(
    card: Card,
    ls: LocalState | None,
    links: list[CardLink],
    *,
    registry: list[dict] | None = None,
) -> dict:
    """``registry`` is forwarded to lane_for_card — see there for what it buys."""
    return {
        "id": card.id,
        "origin": card.origin,
        "external_id": card.external_id,
        "title": card.title,
        "summary": card.summary,
        "url": card.url,
        "ball": card.ball,
        "lane": lane_for_card(
            card.cached or {}, card.ball, settings.jira_hold_label,
            ls.manual_stage if ls else None,
            registry=registry,
        ),
        "driver": card.driver,
        "agent_state": card.agent_state,
        "cached": card.cached,
        "created_at": _iso(card.created_at),
        "updated_at": _iso(card.updated_at),
        "last_seen_at": _iso(card.last_seen_at),
        "local": {
            "read": bool(ls.read) if ls else False,
            "dismissed": bool(ls.dismissed) if ls else False,
            "pinned": bool(ls.pinned) if ls else False,
            "snoozed_until": _iso(ls.snoozed_until) if ls else None,
            "manual_stage": ls.manual_stage if ls else None,
            "picked_option": ls.picked_option if ls else None,
            "picked_at": _iso(ls.picked_at) if ls else None,
            "note": ls.note if ls else None,
            "workdir": ls.workdir if ls else None,
            "claude_session_id": ls.claude_session_id if ls else None,
        },
        "links": [
            {"kind": l.kind, "ref": l.ref, "url": l.url, "title": l.title, "auto": l.auto}
            for l in links
        ],
    }


@router.get("")
async def list_cards(
    include_dismissed: bool = False,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    cards = (await session.execute(select(Card))).scalars().all()
    states = {
        s.card_id: s for s in (await session.execute(select(LocalState))).scalars().all()
    }
    links_by_card: dict[str, list[CardLink]] = {}
    for link in (await session.execute(select(CardLink))).scalars().all():
        links_by_card.setdefault(link.card_id, []).append(link)

    now = datetime.now(timezone.utc)
    # one snapshot for the whole response: every card is bucketed against the SAME lane
    # set even if lanes.json is saved mid-serialize, and the rank sort below reuses it.
    registry = stage_registry()
    out: list[dict] = []
    for card in cards:
        ls = states.get(card.id)
        if ls and not include_dismissed:
            if ls.dismissed:
                continue
            if ls.snoozed_until and ls.snoozed_until > now:
                continue
        out.append(serialize(card, ls, links_by_card.get(card.id, []), registry=registry))

    # newest first, then stable-grouped so Need Human floats to the top. Rank comes
    # from the stage registry (custom stages slot by their declared rank); an unknown
    # lane sinks to the bottom rather than jumping the queue.
    #
    # .get, not [...]: `serialize` already accepts a caller-supplied registry and
    # lane_for_card's arbitration was hardened for the same reason — a hand-built entry
    # missing a field must sink one card to the bottom, not 500 the whole board.
    ranks = {
        lane["key"]: lane.get("rank", 99) for lane in registry if lane.get("key")
    }
    out.sort(key=lambda c: c["updated_at"] or "", reverse=True)
    out.sort(key=lambda c: ranks.get(c["lane"], 99))
    return out


@router.get("/{card_id}")
async def get_card(card_id: str, session: AsyncSession = Depends(get_session)) -> dict:
    card = await session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="card not found")
    ls = await session.get(LocalState, card_id)
    links = (
        await session.execute(select(CardLink).where(CardLink.card_id == card_id))
    ).scalars().all()
    return serialize(card, ls, list(links))
