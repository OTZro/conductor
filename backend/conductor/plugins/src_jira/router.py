"""Jira-owned HTTP endpoints — moved out of ``conductor.api`` (M8 step 3). URLs,
request/response shapes and status codes are all FROZEN, unchanged from the
pre-move ``conductor.api.jira`` / ``conductor.api.pins`` modules and the jira-
only handlers that lived in ``conductor.api.actions`` (``/hold``, ``/awaiting``,
``/resume`` — each already 400'd or no-op'd on a non-jira card, so moving them
changes nothing about which card can call them).

Three distinct URL prefixes (``/api/jira``, ``/api/pins``, ``/api/cards``) means
three ``APIRouter`` objects; the plugin loader only mounts ONE ``ROUTER`` per
module (see ``plugins/files/router.py`` for the one-prefix precedent), so this
module composes them under a bare umbrella router via ``include_router`` — the
mounted paths are byte-identical either way.

``pins`` moved here too, not just the obviously-jira ``/hold``/``/awaiting``/
``/resume``: ``Pin`` (models.Pin) exists solely so a specific ticket key is
always tracked regardless of the board's assignee/JQL filter — src_jira's own
poll is the only reader of the table (see ``impl.py``), and every pin write
immediately calls ``jira_src.fetch_keys`` to seed it. It touches nothing
generic; it is a jira feature start to finish."""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession

from ...actions import resume as resume_action
from ...api.cards import serialize
from ...bus import bus
from ...db import get_session
from ...lanes import stage_registry
from ...models import Card, CardLink, LocalState, Pin, utcnow
from . import impl as jira_src

_jira_key_re = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")  # PROJ-123 shape — guards the acli argv
_pin_key_re = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")

jira_router = APIRouter(prefix="/api/jira", tags=["jira"])
pins_router = APIRouter(prefix="/api/pins", tags=["pins"])
cards_router = APIRouter(prefix="/api/cards", tags=["actions"])


@jira_router.get("/statuses")
async def statuses() -> dict:
    """The board's status vocabulary — what the jira-chip hover offers as transition
    targets (acli can't list a ticket's legal transitions; the transition call below
    validates server-side)."""
    return {"statuses": await jira_src.board_statuses()}


@jira_router.post("/{key}/transition")
async def transition(key: str, payload: dict) -> dict:
    """Move a ticket to another status straight from the chip hover (Building →
    Hardening, …). Surfaces jira's own refusal verbatim on an illegal transition, then
    refreshes the ticket's board card and every card linking it."""
    if not _jira_key_re.match(key):
        raise HTTPException(status_code=400, detail=f"not a jira key: {key}")
    to = str(payload.get("status") or "").strip()
    if not to:
        raise HTTPException(status_code=400, detail="status required")
    try:
        await jira_src.transition_issue(key, to)
    except Exception as exc:  # noqa: BLE001 — jira's reason IS the user feedback
        raise HTTPException(status_code=400, detail=str(exc)[:300])
    # refresh: the ticket's own board card (status/lane move) + hover data on linkers
    try:
        await jira_src.fetch_keys([key])
    except Exception:  # noqa: BLE001 — best-effort; the next poll corrects anyway
        pass
    try:
        await jira_src.refresh_linking_cards(key)
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "key": key, "status": to}


@pins_router.get("")
async def list_pins(session: AsyncSession = Depends(get_session)) -> list[str]:
    return [r.key for r in (await session.execute(select(Pin))).scalars().all()]


@pins_router.post("")
async def add_pin(payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    key = (payload.get("key") or "").strip().upper()
    if not _pin_key_re.match(key):
        raise HTTPException(status_code=400, detail="expected a ticket key like PROJ-123")
    if await session.get(Pin, key) is None:
        session.add(Pin(key=key, created_at=utcnow()))
        await session.commit()
    # fetch it now so it shows without waiting for the next poll
    try:
        await jira_src.fetch_keys([key])
    except Exception:  # noqa: BLE001
        pass
    return {"ok": True, "key": key}


@pins_router.delete("/{key}")
async def remove_pin(key: str, session: AsyncSession = Depends(get_session)) -> dict:
    pin = await session.get(Pin, key.upper())
    if pin:
        await session.delete(pin)
        await session.commit()
    return {"ok": True, "key": key.upper()}


async def _load(session: AsyncSession, card_id: str) -> Card:
    card = await session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="card not found")
    return card


async def _serialize_current(
    session: AsyncSession, card: Card, *, registry: list[dict] | None = None
) -> dict:
    """Mirrors ``conductor.api.actions._serialize_current`` exactly (same helper,
    duplicated rather than imported to avoid a plugin depending on another
    endpoint module's private helper)."""
    ls = await session.get(LocalState, card.id)
    links = (
        await session.execute(select(CardLink).where(CardLink.card_id == card.id))
    ).scalars().all()
    return serialize(card, ls, list(links), registry=registry)


@cards_router.post("/{card_id}/hold")
async def set_hold(card_id: str, payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    """Toggle the hold label on a jira card's ticket — add it to park the
    ticket for a human (→ Need Human), remove it to release."""
    card = await _load(session, card_id)
    if card.origin != "jira":
        raise HTTPException(status_code=400, detail="only jira cards can be held")
    registry = stage_registry()
    await jira_src.set_hold(card.external_id, bool(payload.get("hold", True)), registry=registry)
    await session.refresh(card)  # set_hold committed in its own session
    return await _serialize_current(session, card, registry=registry)


@cards_router.get("/{card_id}/awaiting")
async def awaiting(card_id: str, session: AsyncSession = Depends(get_session)) -> dict | None:
    card = await _load(session, card_id)
    if card.origin != "jira":
        return None
    comments = await resume_action.fetch_comments(card.external_id)
    return resume_action.parse_awaiting(comments)


@cards_router.post("/{card_id}/resume")
async def resume(card_id: str, payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    card = await _load(session, card_id)
    answer = (payload.get("answer") or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="answer required")
    if card.origin != "jira":
        raise HTTPException(status_code=400, detail="resume is only for jira cards")

    await resume_action.post_answer_and_release(card.external_id, answer)

    ls = await session.get(LocalState, card.id)
    if ls is None:
        ls = LocalState(card_id=card.id)
        session.add(ls)
    ls.picked_option = answer[:500]
    ls.picked_at = utcnow()
    ls.read = True
    await session.commit()
    bus.publish({"type": "card.upsert", "id": card.id})
    return {"ok": True}


router = APIRouter()
router.include_router(jira_router)
router.include_router(pins_router)
router.include_router(cards_router)
