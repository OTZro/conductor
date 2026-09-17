"""Slack-owned HTTP endpoint — moved out of ``conductor.api.actions`` (M8 step
3). URL and response shape are FROZEN, unchanged from the pre-move handler (it
already returned ``[]`` for any non-slack card, so moving it changes nothing
about which card can call it)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from ...db import get_session
from ...models import Card
from . import impl as slack_src

router = APIRouter(prefix="/api/cards", tags=["actions"])


@router.get("/{card_id}/slack-thread")
async def slack_thread(card_id: str, session: AsyncSession = Depends(get_session)) -> list[dict]:
    """The full slack thread behind this card (sender + text per message) so the whole
    discussion shows in-app. Empty for non-slack cards."""
    card = await session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="card not found")
    if card.origin != "slack":
        return []
    return await slack_src.fetch_thread(card)
