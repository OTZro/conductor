"""Files plugin router — the two endpoints the plugin owns:

- ``POST /capture``  the PostToolUse/SendUserFile hook target. Core forwards the raw hook
  payload here (see terminal.py ``_plugin_hooks``); we pull ``tool_input.files`` (paths on
  the SESSION's machine) and read each via ``_run(head -c, host)`` — uniform whether the
  session is local or over ssh, so the backend never needs the remote bytes pushed to it.
- ``GET /{card_id}/{name}``  serves a stashed file's bytes, scoped to the card's own dir
  with a path-traversal guard (both segments are attacker-influenced; this route is
  session-gated but treats its input as untrusted anyway).

Both are token-guarded via the shared ``auth_service.require_ingest_token`` (``/capture``
is machine-push and session-exempt; the download rides the normal session gate)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from conductor import db, store
from conductor.actions.runner import _run
from conductor.auth import service as auth_service
from conductor.models import Card, utcnow

from . import storage

router = APIRouter(prefix="/api/plugins/files", tags=["files"])


@router.post("/capture")
async def capture(request: Request, card_id: str, host: str = "") -> dict:
    """Stash the files a card's claude just sent via SendUserFile. ``card_id`` + ``host``
    ride as query params (set by the generated hook); the body is the raw PostToolUse
    payload. ``host=''`` → the session is local; a name → read over ssh. An unreadable or
    oversize file is skipped rather than failing the whole hook (a hook can't surface an
    error to the user). ``touch_card`` refreshes an open drawer + re-sorts the board."""
    auth_service.require_ingest_token(request)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001 — a malformed hook body shouldn't 500 the hook
        return {"ok": True, "saved": []}
    # reject an unknown card before reading/stashing anything — no orphan file dirs from a
    # stray call. Short session (closed before the possibly-slow ssh reads below).
    async with db.session_maker() as session:
        if await session.get(Card, card_id) is None:
            raise HTTPException(status_code=404, detail="card not found")
    tool_input = (payload or {}).get("tool_input") or {}
    caption = str(tool_input.get("caption") or "").strip()
    saved: list[str] = []
    for path in tool_input.get("files") or []:
        if not path:
            continue
        # head -c (cap+1) bounds memory AND detects oversize in one read — cat would pull
        # an unbounded file fully into RAM before any size check could run.
        rc, out = await _run(["head", "-c", str(storage.MAX_FILE + 1), path], host=host or None, timeout=20)
        if rc != 0 or not out or len(out) > storage.MAX_FILE:
            continue
        entry = storage.record_file(
            card_id, out, source=path, caption=caption, sent_at=utcnow().isoformat()
        )
        saved.append(entry["name"])
    if saved:
        async with db.session_maker() as session:
            await store.touch_card(session, card_id)
    return {"ok": True, "saved": saved}


@router.get("/{card_id}/{name}")
async def download(card_id: str, name: str) -> FileResponse:
    root = storage.FILES_DIR.resolve()
    target = (root / card_id / name).resolve()
    # assert the resolved path stays under FILES_DIR — catches ../ traversal in either
    # card_id or name. is_relative_to needs py3.9+.
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(target, filename=target.name)
