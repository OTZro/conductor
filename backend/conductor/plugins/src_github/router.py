"""GitHub-owned HTTP endpoint — moved out of ``conductor.api.actions`` (M8 step
3). URL, request/response shape and status codes are FROZEN, unchanged from
the pre-move handler (it already 400'd any card with no PR reference, so
moving it changes nothing about which card can call it)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel.ext.asyncio.session import AsyncSession

from ...actions import pr as pr_action
from ...db import get_session
from ...models import Card
from . import impl as github_src

log = logging.getLogger("conductor.plugins.src_github")

router = APIRouter(prefix="/api/cards", tags=["actions"])


@router.post("/{card_id}/pr/{action}")
async def pr(card_id: str, action: str, session: AsyncSession = Depends(get_session)) -> dict:
    card = await session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="card not found")
    gh = (card.cached or {}).get("github") or {}
    repo = gh.get("repo")
    number = gh.get("number")
    if (not repo or number is None) and "#" in card.external_id:
        repo, num = card.external_id.rsplit("#", 1)
        number = int(num) if num.isdigit() else None
    if not repo or number is None:
        raise HTTPException(status_code=400, detail="card has no PR reference")

    if action == "approve":
        result = await pr_action.approve(repo, int(number))
    elif action == "merge":
        result = await pr_action.merge(repo, int(number))
    else:
        raise HTTPException(status_code=400, detail=f"unknown action: {action}")

    # Write the outcome back onto the card. Without this the action changes GitHub and
    # nothing else: the board renders from `cached`, so an approve leaves the chip on
    # the automated tool's SUGGEST_APPROVE and a merge leaves the card in Need Human
    # until the dates loop catches up — and the frontend refetches immediately after,
    # so it reads as "checked, nothing changed" rather than "not looked at yet".
    # Deliberately after `result` is in hand and deliberately swallowed: the PR action
    # already succeeded, and failing the request over a stale badge would report the
    # action as failed when it landed. See impl.refresh_pr_card.
    try:
        await github_src.refresh_pr_card(card.origin, card.external_id, repo, int(number))
    except Exception as exc:  # noqa: BLE001
        log.warning("[pr:%s] %s succeeded but the card write-back failed: %s",
                    card.external_id, action, exc)
    return result
