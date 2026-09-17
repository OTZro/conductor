"""Fork plugin — duplicate a card's live claude conversation into a second tmux window.

Rides claude's own ``/fork`` slash command ("copy this conversation into a new
background session and keep working here"): the live pane is untouched — we type
``/fork`` into it, parse the confirmation for the forked session's short id, resolve
the full session id from the transcript store, and open a SECOND conductor-managed
terminal resuming the fork (core's ordinary resume machinery: tmux + ttyd +
TerminalSession row). Result: two windows — the original keeps working, the fork
explores a different direction from the same history.

UI: a row in the terminal's ⋯ menu (card_widget ``slot="menu"``). The provider offers
it only when the card has a forkable session (a conductor-owned tmux with a claude
conversation); the endpoint re-validates liveness and refuses mid-turn panes (claude
only honors slash commands at an idle prompt)."""

from __future__ import annotations

from sqlalchemy import select

from conductor import db  # resolve session_maker at CALL time (tests swap db.session_maker)
from conductor.models import Card

from ..base import CardWidgetSpec, Plugin
from .router import forkable_session
from .router import router as ROUTER  # noqa: F401 — mounted by the loader


async def _card_data(ctx: dict) -> dict | None:
    """Offer the fork row only when the card has a session that COULD fork — a
    conductor-owned row carrying both a tmux name and a claude conversation. Liveness
    is re-checked by the endpoint (a provider runs on every card open; a tmux probe
    there would be per-open cost for a menu row)."""
    origin, ext = ctx.get("origin"), ctx.get("external_id")
    if not origin or not ext:
        return None
    async with db.session_maker() as s:
        card = (
            await s.execute(select(Card).where(Card.origin == origin, Card.external_id == ext))
        ).scalar_one_or_none()
        if card is None:
            return None
        row = await forkable_session(s, card.id)
    if row is None:
        return None
    return {"card_id": card.id}


PLUGIN = Plugin(
    id="fork",
    label="Fork",
    icon="⑂",
    card_widget=CardWidgetSpec(title="Fork", layout="fork-menu", provider=_card_data, slot="menu"),
)
