"""Fork endpoint — drives claude's ``/fork`` in the card's live pane, then opens a
second conductor terminal on the forked conversation. See ``__init__`` for the flow."""

from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from conductor import db, store
from conductor.actions import agent_state
from conductor.actions import terminal as term
from conductor.actions.runner import _run
from conductor.models import Card, TerminalSession, utcnow

router = APIRouter(prefix="/api/plugins/fork", tags=["fork"])

# the /fork confirmation line: "⎿  session waiting for a prompt · <summary> · c51d1118"
# (live-captured 2026-07-22). The trailing 8-hex token is the forked session's short id.
_FORK_ID_RE = re.compile(r"⎿.*·\s*([0-9a-f]{8})\b")


async def forkable_session(session, card_id: str) -> TerminalSession | None:
    """The card's newest conductor-owned session that could fork: has a tmux to type
    into and a claude conversation behind it. Shared with the plugin's provider."""
    rows = (
        (
            await session.execute(
                select(TerminalSession).where(TerminalSession.card_id == card_id)
            )
        )
        .scalars()
        .all()
    )
    live = [
        r for r in rows
        if r.kind in ("own", "resume", "takeover") and r.tmux_session and r.claude_session_id
    ]
    live.sort(key=lambda r: (r.created_at is not None, r.created_at), reverse=True)
    return live[0] if live else None


async def _resolve_full_sid(short: str, host: str | None) -> str | None:
    """Full session UUID from its 8-hex prefix — the transcript file's basename under
    ~/.claude/projects (same store core's resume machinery reads). Retried briefly:
    the fork's transcript materializes moments after the confirmation renders."""
    find = (
        f"find ~/.claude/projects -maxdepth 2 -name '{short}*.jsonl' 2>/dev/null | head -1"
    )
    for _ in range(5):
        rc, out = await _run(["sh", "-c", find], host=host)
        path = out.decode(errors="replace").strip()
        if rc == 0 and path:
            return path.rsplit("/", 1)[-1].removesuffix(".jsonl")
        await asyncio.sleep(0.8)
    return None


@router.post("/{card_id}")
async def fork(card_id: str) -> dict:
    """Type ``/fork`` into the card's live pane (untouched — the fork goes to a
    background session), then open a second terminal resuming the fork. 409 when
    there's nothing live to fork or the pane is mid-turn (claude only honors slash
    commands at an idle prompt)."""
    async with db.session_maker() as session:
        card = await session.get(Card, card_id)
        if card is None:
            raise HTTPException(status_code=404, detail="card not found")
        row = await forkable_session(session, card_id)
    if row is None:
        raise HTTPException(status_code=409, detail="no forkable claude session on this card")
    name, host = row.tmux_session, row.host
    # serialize the check-then-act tmux sequence per card — two concurrent /fork
    # requests interleaving their tmux send-keys could otherwise race into duplicate
    # forked conversations or garbled confirmation parsing. Scoped to just this part:
    # store.touch_card below takes the same per-card lock itself, and asyncio.Lock
    # isn't reentrant, so it must run after this block releases it.
    async with store._card_lock(card.origin, card.external_id):
        if not await term._tmux_has_session(name, host):
            raise HTTPException(status_code=409, detail=f"tmux session '{name}' is not running")
        state = await agent_state._classify_pane(name, host=host)
        if state and state[0] == "running":
            raise HTTPException(
                status_code=409, detail="claude is mid-turn — fork needs an idle prompt"
            )

        # type /fork, let the command palette highlight it, execute. The pane keeps the
        # ORIGINAL conversation; the fork lands in a background session.
        await _run(["tmux", "send-keys", "-t", name, "/fork"], host=host)
        await asyncio.sleep(1.2)
        await _run(["tmux", "send-keys", "-t", name, "Enter"], host=host)
        short, pane_text = None, ""
        for _ in range(6):  # confirmation renders within a few seconds
            await asyncio.sleep(1.0)
            rc, out = await _run(["tmux", "capture-pane", "-p", "-t", name], host=host)
            if rc != 0:
                continue
            pane_text = out.decode(errors="replace")
            hits = _FORK_ID_RE.findall(pane_text)
            if hits:
                short = hits[-1]  # the newest confirmation on screen
                break
        if not short:
            # surface claude's OWN refusal (e.g. "Nothing to fork yet. Send a message
            # first.") — the ⎿ result line right after the last /fork echo.
            why = ""
            lines = pane_text.splitlines()
            for i in range(len(lines) - 1, -1, -1):
                if lines[i].strip().endswith("/fork"):
                    for after in lines[i + 1 :]:
                        s = after.strip()
                        if s.startswith("⎿"):
                            why = s.lstrip("⎿").strip()
                            break
                    break
            raise HTTPException(
                status_code=409 if why else 502,
                detail=why or "sent /fork but no fork confirmation appeared on the pane",
            )
        full_sid = await _resolve_full_sid(short, host)
        if not full_sid:
            raise HTTPException(
                status_code=502, detail=f"fork {short} confirmed but its transcript was not found"
            )

    # window 2: core's ordinary resume machinery on the FORKED conversation — tmux +
    # ttyd + a TerminalSession row, so it shows up in the card's terminal list. Outside
    # the fork lock above: this drives a NEW terminal on the resolved fork, not the
    # original pane, so it isn't part of that race.
    async with db.session_maker() as session:
        ts = TerminalSession(
            card_id=card_id, kind="resume", host=host, status="starting", created_at=utcnow()
        )
        session.add(ts)
        await session.flush()
        try:
            info = await term.open_terminal(
                session_id=ts.id,
                origin=card.origin,
                external_id=card.external_id,
                kind="resume",
                card_id=card_id,
                claude_session_id=full_sid,
                host=host,
            )
        except Exception as exc:  # surface the reason, mark the row; chained via `from exc`
            ts.status = "error"
            await session.commit()
            raise HTTPException(
                status_code=502, detail=f"fork created but open failed: {exc}"
            ) from exc
        ts.ttyd_port = info["port"]
        ts.url = info["url"]
        ts.tmux_session = info["tmux_session"]
        ts.pid = info["pid"]
        ts.cwd = info["cwd"]
        ts.claude_session_id = info["claude_session_id"]
        ts.status = "live"
        await session.commit()
        await store.touch_card(session, card_id)  # open drawer refreshes its session list
    return {"ok": True, "fork": short, "claude_session_id": full_sid, "url": info["url"],
            "tmux_session": info["tmux_session"]}
