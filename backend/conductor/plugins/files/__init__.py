"""Files plugin — surfaces files a card's claude sent via SendUserFile, END TO END with
no core change beyond the generic hook framework:

- it DECLARES a claude hook (``HookSpec`` below): core folds "PostToolUse / SendUserFile →
  POST /api/plugins/files/capture" into every session's generated hook settings and
  auto-exempts that path from the auth gate — no per-plugin wiring in core;
- its ROUTER owns ``/capture`` (stash) + the download route, and ``storage`` owns the
  on-disk location + metadata sidecar (core has no ``files_dir`` / ``cached.files``);
- this module contributes the card-detail widget, reading the plugin's own store.

The only thing core lends is ``store.touch_card`` — the generic "a card changed
out-of-band, refresh it" primitive the capture endpoint calls."""

from __future__ import annotations

from sqlalchemy import select

from conductor import db  # resolve session_maker at CALL time (tests swap db.session_maker)
from conductor.models import Card

from ..base import CardWidgetSpec, HookSpec, Plugin
from . import storage
from .router import router as ROUTER  # noqa: F401 — auto-mounted by the plugin loader


async def _card_data(ctx: dict) -> dict | None:
    """This card's received files, or None if it has none. ctx carries origin +
    external_id but NOT the internal card id (which the download URL and the file store
    are both keyed by), so look the card up for its id, then read the plugin's store."""
    origin, ext = ctx.get("origin"), ctx.get("external_id")
    if not origin or not ext:
        return None
    async with db.session_maker() as s:
        card = (
            await s.execute(select(Card).where(Card.origin == origin, Card.external_id == ext))
        ).scalar_one_or_none()
    if card is None:
        return None
    files = storage.list_files(card.id)
    if not files:
        return None
    # project to the display fields — `source` (the sender's local path) is a dedup-only
    # detail and must not leak to the browser.
    public = [
        {"name": f["name"], "caption": f.get("caption"), "size": f.get("size"), "sent_at": f.get("sent_at")}
        for f in files
    ]
    return {"card_id": card.id, "files": public}


PLUGIN = Plugin(
    id="files",
    label="Files",
    icon="📎",
    card_widget=CardWidgetSpec(title="Files", layout="files-card", provider=_card_data),
    # the whole point of the hook framework: declare the hook, touch no core file
    hooks=(HookSpec(event="PostToolUse", matcher="SendUserFile", path="/api/plugins/files/capture"),),
)
