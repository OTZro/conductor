"""Stages plugin — the manual "move to stage" control, as a PLUGIN rather than core UI.

Core owns the stage MACHINERY (registry + arbitration in lanes.py, the
LocalState.manual_stage field, and the validated ``PATCH /state {stage}`` write path);
this plugin contributes only the card-detail control that drives it, through the same
card-widget slot every plugin uses. The provider owns the "does this apply?" gate: it
returns None while the registry carries no custom human-space stage, so a stock
install (built-ins only) renders nothing anywhere. Removing this plugin removes the
button, not the capability — plugin claims and the PATCH API keep working."""

from __future__ import annotations

from sqlalchemy import select

from conductor import db  # resolve session_maker at CALL time (tests swap db.session_maker)
from conductor.lanes import stage_registry
from conductor.models import Card, LocalState

from ..base import CardWidgetSpec, Plugin


async def _card_data(ctx: dict) -> dict | None:
    """The card's move-to-stage choices + its current manual override, or None when the
    board is stock (no custom human stage) — moving between just the built-ins is noise.
    Once any custom stage exists, ALL human-space stages (built-ins included) are offered
    for full manual control; "Auto" (clearing) is the frontend's affair."""
    origin, ext = ctx.get("origin"), ctx.get("external_id")
    if not origin or not ext:
        return None
    human = [lane for lane in stage_registry() if lane["within"] == "human"]
    if not any(not lane["builtin"] for lane in human):
        return None
    async with db.session_maker() as s:
        card = (
            await s.execute(select(Card).where(Card.origin == origin, Card.external_id == ext))
        ).scalar_one_or_none()
        if card is None:
            return None
        ls = await s.get(LocalState, card.id)
    return {
        "card_id": card.id,
        "current": ls.manual_stage if ls else None,
        "choices": [{"key": lane["key"], "title": lane["title"]} for lane in human],
    }


PLUGIN = Plugin(
    id="stages",
    label="Stages",
    icon="⇢",
    # actions slot → renders inline in the card detail's header action row
    # (next to Snooze/Pin), not as a body section
    card_widget=CardWidgetSpec(
        title="Stage", layout="stage-move", provider=_card_data, slot="actions"
    ),
)
