from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from .. import launch_profiles, prompts
from ..actions import terminal as term
from ..config import settings
from ..db import get_session
from ..models import Card, LocalState, TerminalSession, new_id, utcnow
from ..store import upsert_card

router = APIRouter(prefix="/api", tags=["terminals"])

_KINDS = {"attach", "own", "resume"}


def _norm_host(value: str | None) -> str | None:
    """Payload host → canonical: local name/blank → None, a configured remote name
    → itself, anything else → 400."""
    if value is not None and not isinstance(value, str):
        # a JSON number/object would raise AttributeError on .strip() → 500; be explicit
        raise HTTPException(status_code=400, detail="host must be a string")
    h = (value or "").strip()
    if not h or h == settings.local_host_name:
        return None
    if h not in settings.remote_host_map:
        raise HTTPException(status_code=400, detail=f"unknown host '{h}'")
    return h


@router.get("/hosts")
async def list_hosts() -> list[dict]:
    """Machines a claude session can run on, with live reachability (drives the
    host picker + chips)."""
    out = [{"name": settings.local_host_name, "local": True, "online": True}]
    for name in settings.remote_host_map:
        out.append({"name": name, "local": False, "online": await term.reachable(name)})
    return out


@router.get("/cards/{card_id}/default-cwd")
async def card_default_cwd(
    card_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """The dir a NEW session defaults to for this card (CONDUCTOR_DEFAULT_WORKSPACE_ROOT
    → $HOME) so the New-session field can pre-fill the real configured default instead
    of a hardcoded path. Blanking the field resolves to the same value server-side."""
    card = await session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="card not found")
    return {"cwd": term.default_cwd(card.origin, card.external_id)}


@router.get("/dirs")
async def list_dirs(path: str = "", host: str | None = None) -> dict:
    """Directory existence + completions for the New-session working-dir field, checked
    on the given host (local or remote over ssh). {valid, dirs}."""
    return await term.list_dirs(path, _norm_host(host))


@router.get("/launch-profiles")
async def list_launch_profiles() -> list[dict]:
    """Named launch presets from ~/.conductor/profiles.json for the New-session picker:
    each {name, host, cwd}. Empty when the user has defined none."""
    return launch_profiles.summaries()


@router.post("/cards/{card_id}/terminal")
async def open_terminal(
    card_id: str, payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    card = await session.get(Card, card_id)
    if not card:
        raise HTTPException(status_code=404, detail="card not found")
    kind = payload.get("kind", "own")
    if kind not in _KINDS:
        raise HTTPException(status_code=400, detail=f"kind must be one of {_KINDS}")

    ls = await session.get(LocalState, card.id)
    if ls is None:
        ls = LocalState(card_id=card.id)
        session.add(ls)

    # a NEW session may draw host/cwd/env from a named launch profile; resume/attach
    # continue an existing session, so they ignore profiles (they pin their own).
    prof: dict = {}
    raw_profile = payload.get("profile")
    if raw_profile is None:
        pname = ""
    elif not isinstance(raw_profile, str):  # false/0/[]/{} etc. → explicit 400, not silently ignored
        raise HTTPException(status_code=400, detail="profile must be a string")
    else:
        pname = raw_profile.strip()
    if kind == "own" and pname:
        resolved = launch_profiles.resolve(pname)
        if resolved is None:
            raise HTTPException(status_code=400, detail=f"unknown launch profile '{pname}'")
        prof = resolved
    # Where to run: attach is always local (its watched tmux lives on this machine by
    # definition). resume follows the machine the conversation lives on (claude session
    # files never move across hosts). own honors the picker. PRESENCE check, not
    # truthiness: ""/local name is an EXPLICIT "base", distinct from key-absent (→ the
    # card's remembered host) — truthiness here once sent base conversations to roam
    # (the PROJ-6595 bug).
    if kind == "attach":
        host = None
    elif kind == "resume":
        host = _norm_host(payload.get("host")) if "host" in payload else ls.host
    else:  # own — profile supplies host when the request omits it
        host = _norm_host(payload.get("host") if "host" in payload else prof.get("host"))
    # request > profile > remembered. An explicit "cwd" (even "") wins — blank means
    # "use the configured default", which a profile cwd must not override.
    cwd = (
        (payload.get("cwd") or None)
        if "cwd" in payload
        else prof.get("cwd") or (ls.workdir if (ls.host or None) == host else None)
    )
    env = prof.get("env") or None
    # a real JSON bool only — `bool("false")` is True, so coercing a stringified bool
    # would silently disable revival (the auto-open path always sends a real boolean).
    attach_only = payload.get("attach_only", False)
    if not isinstance(attach_only, bool):
        raise HTTPException(status_code=400, detail="attach_only must be a boolean")
    claude_sid = payload.get("claude_session_id") or (
        ls.claude_session_id if kind == "resume" else None
    )
    # generalized Watch target (see term._build_command's `watch` param): any plugin
    # (e.g. an agent-team plugin) can make its cards watchable by writing cached.watch =
    # {"session": str, "socket": str|None, "host": str|None, "writable": bool} — kind
    # "attach" attaches that named tmux directly. writable defaults True (read-write);
    # the declarer sets it False for a read-only view (such a plugin does).
    # Type-checked, not just truthy: cached is a free-form JSON blob a plugin controls.
    raw_watch = (card.cached or {}).get("watch")
    watch = raw_watch if isinstance(raw_watch, dict) else None

    # for a slack card, seed a FRESH session with the message + brief so the user drops
    # straight into a context-aware claude. Only for kind=="own": resume continues an
    # existing conversation, which already has context, so re-seeding would post the
    # mention prompt again (a duplicate). an explicit prompt (e.g. the handover pickup)
    # wins; else a slack card seeds its own
    initial_prompt = payload.get("initial_prompt") or None
    if not initial_prompt and card.origin == "slack" and kind == "own":
        sl = (card.cached or {}).get("slack") or {}
        bits = []
        if sl.get("text"):
            bits.append(prompts.prompt("seed_slack_mention", text=sl["text"]))
        if card.url:
            bits.append(prompts.prompt("seed_slack_link", url=card.url))
        if sl.get("brief"):
            bits.append(prompts.prompt("seed_slack_gist", brief=sl["brief"]))
        if bits:
            bits.append(prompts.prompt("seed_slack_closing"))
            initial_prompt = "\n\n".join(bits)

    ts = TerminalSession(
        card_id=card.id, kind=kind, host=host, status="starting", created_at=utcnow()
    )
    session.add(ts)
    await session.flush()

    try:
        info = await term.open_terminal(
            session_id=ts.id,
            origin=card.origin,
            external_id=card.external_id,
            kind=kind,
            card_id=card.id,
            cwd=cwd,
            claude_session_id=claude_sid,
            font_size=payload.get("font_size"),
            initial_prompt=initial_prompt,
            host=host,
            # auto-open on card view sends this: attach a live session, never revive a
            # killed one (only the explicit Resume button / conversation list revives).
            attach_only=attach_only,
            env=env,  # launch-profile env overrides (own sessions only; local-only)
            watch=watch,
            # xterm palette for THIS viewer — the browser sends its active theme's
            # terminal colours (see frontend theme.ts). Sanitised in term._theme_args.
            term_theme=payload.get("term_theme"),
            # theme's tmux styles: {} means "unstyle" (restore the user's tmux.conf),
            # absent means "no opinion". Applied only to sessions conductor owns.
            tmux_style=payload.get("tmux_style"),
        )
    except Exception as exc:  # noqa: BLE001
        ts.status = "error"
        await session.commit()
        raise HTTPException(status_code=400, detail=str(exc))

    ts.ttyd_port = info["port"]
    ts.url = info["url"]  # same-origin proxy path (see api/termproxy.py)
    ts.tmux_session = info["tmux_session"]
    ts.pid = info["pid"]
    ts.cwd = info["cwd"]
    ts.claude_session_id = info["claude_session_id"]
    ts.status = "live"
    # one row per conversation — a conversation belongs to ONE card at a time, and an
    # EXPLICIT open (resume / own) transfers ownership to this card: rows
    # for the same conversation/tmux on OTHER cards are dropped too. Without that,
    # the pane poll (which maps a tmux to every card holding a row) let one running
    # pane drive several cards' agent state at once (PROJ-3783 sat in AI Working
    # because PROJ-10367 re-opened the same conversation and the pane was busy).
    # A PASSIVE auto-attach (attach_only — the drawer reconnecting on view) must NOT
    # steal: two cards remembering the same conversation would ping-pong ownership on
    # every drawer open. It prunes only its own card's stale rows.
    # Matched in the WHERE clause (not loaded-then-filtered in Python) — this runs on
    # every open, including passive attach-only ones, so it must not scan the whole table.
    dup_conditions = []
    if ts.claude_session_id:
        dup_conditions.append(TerminalSession.claude_session_id == ts.claude_session_id)
    if ts.tmux_session:
        dup_conditions.append(TerminalSession.tmux_session == ts.tmux_session)
    old_rows = []
    if dup_conditions:
        dup = select(TerminalSession).where(TerminalSession.id != ts.id, or_(*dup_conditions))
        old_rows = (await session.execute(dup)).scalars().all()
    for old in old_rows:
        if old.card_id != card.id and attach_only:
            continue  # passive view — ownership stays where it was
        if old.card_id != card.id and ts.claude_session_id:
            # the losing card must FORGET this conversation too — its
            # LocalState.claude_session_id pointer outlives the row, so otherwise its
            # next auto-open re-resumes and re-binds it (exactly how PROJ-3783 kept
            # re-adopting c853b41e after PROJ-10367 explicitly took it over). Only when
            # the pointer still names THIS conversation — a card pointing elsewhere is
            # left alone.
            other_ls = await session.get(LocalState, old.card_id)
            if other_ls and other_ls.claude_session_id == ts.claude_session_id:
                other_ls.claude_session_id = None
                other_ls.host = None
        await session.delete(old)
    # remember context on the card for next time / resume after close — host included,
    # since the claude conversation only resumes on the machine that holds it
    if info["cwd"]:
        ls.workdir = info["cwd"]
    if info["claude_session_id"] and kind in ("own", "resume"):
        ls.claude_session_id = info["claude_session_id"]
        ls.host = host
    await session.commit()
    return {
        "id": ts.id,
        "card_id": card.id,
        "kind": kind,
        "url": ts.url,
        "status": ts.status,
        "cwd": ts.cwd,
        "claude_session_id": ts.claude_session_id,
        # the tmux this terminal is showing. Returned rather than left to the client to
        # rebuild: the naming rule is the backend's (`conductor-<uuid8>` for own/resume;
        # attach's name comes from the plugin's cached.watch), and a second copy drifts.
        "tmux_session": ts.tmux_session,
        "host": host,
    }


@router.delete("/terminals/{session_id}")
async def close_terminal(
    session_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """Stop the ttyd VIEWER only. Deliberately does not touch the card's agent
    state: the tmux+claude usually outlives the view, and the pane poll owns the
    lifecycle (clears within one cycle once sessions really die). Clearing here
    used to bounce cards Need Human → AI Working on every modal close."""
    stopped = await term.stop_terminal(session_id)
    ts = await session.get(TerminalSession, session_id)
    if ts:
        ts.status = "stopped"
        await session.commit()
    return {"ok": True, "stopped": stopped}


async def _session_target(
    session_id: str, session: AsyncSession
) -> tuple[str | None, str | None]:
    """(tmux_session, host) for a terminal — live _SESSIONS first, then the DB row."""
    name = term.session_tmux(session_id)
    if name:
        return name, term.session_host(session_id)
    ts = await session.get(TerminalSession, session_id)
    return (ts.tmux_session, ts.host) if ts else (None, None)


@router.post("/terminals/{session_id}/kill")
async def kill_terminal(
    session_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """Truly end the claude: stop the ttyd AND kill its tmux session."""
    name, host = await _session_target(session_id, session)
    await term.stop_terminal(session_id)
    killed = False
    if name:
        await term.kill_tmux(name, host)
        killed = True
    ts = await session.get(TerminalSession, session_id)
    if ts:
        ts.status = "killed"
        await session.commit()
    return {"ok": True, "killed": killed, "tmux_session": name}


@router.post("/terminals/{session_id}/scroll")
async def scroll_terminal(
    session_id: str, payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    """Scroll a terminal's tmux scrollback. dir = up|down|exit; lines batches a whole
    gesture into one request (one pane redraw). Returns where it landed, so the browser
    reads its scroll depth off tmux instead of counting what it sent."""
    name, host = await _session_target(session_id, session)
    if not name:
        raise HTTPException(status_code=404, detail="no tmux session for this terminal")
    try:
        state = await term.scroll_tmux(
            name, (payload.get("dir") or "up"), host, payload.get("lines") or 1
        )
    except Exception as exc:  # noqa: BLE001 — an offline remote must not read as "live"
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, **state}


@router.get("/terminals/{session_id}/capture")
async def capture_terminal(
    session_id: str, session: AsyncSession = Depends(get_session)
) -> dict:
    """Return the terminal's pane text so the browser can offer copy (tmux mouse
    mode otherwise sends drag-selections to the server buffer, not the clipboard)."""
    name, host = await _session_target(session_id, session)
    if not name:
        raise HTTPException(status_code=404, detail="no tmux session for this terminal")
    return {"text": await term.capture_tmux(name, host)}


@router.post("/terminals/{session_id}/paste-image")
async def paste_image(
    session_id: str, payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    """Land a clipboard image (base64 / data-URL) on the session's HOST and type its
    path into the pane — how a Roam-clipboard image reaches a Base claude when the two
    only share a tailnet. The browser (on the viewer's machine) supplies the bytes."""
    import base64

    name, host = await _session_target(session_id, session)
    if not name:
        raise HTTPException(status_code=404, detail="no tmux session for this terminal")
    raw = str(payload.get("data") or "")
    if "," in raw and raw.lstrip().startswith("data:"):
        raw = raw.split(",", 1)[1]  # strip a data-URL prefix
    try:
        data = base64.b64decode(raw, validate=False)
    except Exception:
        raise HTTPException(status_code=400, detail="bad image data")
    if not data:
        raise HTTPException(status_code=400, detail="empty image")
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="image too large (>25MB)")
    ext = {
        "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp",
    }.get(payload.get("mime") or "image/png", "png")
    try:
        path = await term.paste_image(name, host, data, ext)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))
    return {"ok": True, "path": path}


@router.get("/tmux")
async def list_tmux(
    status: bool = False, session: AsyncSession = Depends(get_session)
) -> list[dict]:
    """All live tmux sessions, each linked back to its card when resolvable
    (conductor-<8> → a TerminalSession / LocalState claude session). `?status=1`
    additionally captures each claude pane for live state/model/ctx/task (Terminals
    sidebar only — the plain list stays cheap for callers that just need names)."""
    sessions = await term.list_tmux()
    if not sessions:
        return []

    cards = (await session.execute(select(Card))).scalars().all()
    card_by_id = {c.id: c for c in cards}
    by_tmux = {
        t.tmux_session: t.card_id
        for t in (await session.execute(select(TerminalSession))).scalars().all()
        if t.tmux_session
    }
    by_claude8 = {
        ls.claude_session_id[:8]: ls.card_id
        for ls in (await session.execute(select(LocalState))).scalars().all()
        if ls.claude_session_id
    }

    out = []
    for s in sessions:
        name = s["name"]
        if name.startswith("conductor-"):
            cid = by_tmux.get(name) or by_claude8.get(name[len("conductor-"):])
        else:
            # a card-less session (shell-*) adopted onto a card carries a TerminalSession
            # row under its own name — resolve it the same as a conductor session.
            cid = by_tmux.get(name)
        card = card_by_id.get(cid) if cid else None
        s = dict(s)
        if card:
            s["card_id"] = card.id
            s["external_id"] = card.external_id
            s["title"] = card.title
            s["origin"] = card.origin
        out.append(s)

    if status:
        import asyncio

        # conductor sessions are known-claude → always show status. 'other' (card-less
        # shells) may hold a hand-run claude → capture too, but only attach the status
        # when the pane actually looks like claude, so a plain bash/vim shell doesn't
        # get a misleading idle dot. capture-pane rides one round-trip, cached ~6s.
        cap = [s for s in out if s["kind"] in ("conductor", "other")]
        stats = await asyncio.gather(
            *(term.session_status(s["name"], s.get("host")) for s in cap)
        )
        for s, st in zip(cap, stats):
            if not st:
                continue
            if s["kind"] == "other" and not st.get("is_claude"):
                continue
            s["status"] = st
    return out


@router.get("/cards/{card_id}/conversations")
async def list_conversations(
    card_id: str, session: AsyncSession = Depends(get_session)
) -> list[dict]:
    """This card's past claude conversations (from TerminalSession history), newest
    first, deduped by claude_session_id. A killed tmux does NOT end the conversation —
    `claude --resume <sid>` revives it in a fresh tmux — so dead ones are offered too;
    `live` just says whether its conductor-<sid8> tmux is up right now (on its host)."""
    rows = (
        await session.execute(
            select(TerminalSession)
            .where(TerminalSession.card_id == card_id)
            .order_by(TerminalSession.created_at.desc())
        )
    ).scalars().all()
    live = {(s.get("host"), s["name"]) for s in await term.list_tmux()}
    out: list[dict] = []
    seen: set[str] = set()
    for r in rows:
        sid = r.claude_session_id
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append({
            "claude_session_id": sid,
            "host": r.host,
            "live": (r.host, f"conductor-{sid[:8]}") in live,
            "last_used": r.created_at.isoformat() if r.created_at else None,
        })
    return out


@router.post("/tmux/new")
async def new_tmux(payload: dict) -> dict:
    """Create a fresh non-conductor shell tmux session on a host and open it — a plain
    terminal on Base or Roam, unrelated to any card."""
    host = _norm_host(payload.get("host"))
    if host and not await term.reachable(host):
        raise HTTPException(status_code=503, detail=f"host '{host}' is offline")
    return await term.new_shell(host)


@router.post("/tmux/open")
async def open_tmux(payload: dict) -> dict:
    """Attach a ttyd to an existing tmux session by name (card-less). `socket` attaches on
    a non-default `-L <socket>` server (e.g. an agent team's `agents`); writability there
    is decided PER SOCKET by CONDUCTOR_EXTRA_TMUX_SOCKETS (`:rw` opts in, read-only is
    the default) — the same rule term.open_raw_tmux applies."""
    name = payload.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    sock = payload.get("socket")
    if sock is not None and not isinstance(sock, str):
        # same courtesy _norm_host extends to `host`: a malformed payload gets a 400,
        # not a 500 from `-L 5` reaching subprocess argv (or an unhashable map key)
        raise HTTPException(status_code=400, detail="socket must be a string")
    style = payload.get("tmux_style")
    if style is not None and not isinstance(style, dict):
        # _theme_args shrugs off a malformed term_theme, but a truthy non-dict style
        # reaches apply_tmux_style's .items() and turns the payload mistake into a 500
        raise HTTPException(status_code=400, detail="tmux_style must be an object")
    return await term.open_raw_tmux(
        name,
        writable=payload.get("writable", True),
        font_size=payload.get("font_size"),
        host=_norm_host(payload.get("host")),
        socket=sock or None,
        term_theme=payload.get("term_theme"),
        tmux_style=style,
    )


@router.post("/tmux/adopt")
async def adopt_tmux(payload: dict, session: AsyncSession = Depends(get_session)) -> dict:
    """Bind a card-less tmux session (e.g. a claude you started by hand in a shell-*
    session) onto a card. Afterwards it shows under that card in Terminals, drives the
    card's AI-Working / Need-Human lane like a conductor session, and can be resumed
    later. `card_id` optional: omit it to spin up a fresh manual card titled from the
    session's own claude task summary (or its tmux name)."""
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    host = _norm_host(payload.get("host"))
    if host and not await term.reachable(host):
        raise HTTPException(status_code=503, detail=f"host '{host}' is offline")
    if not await term._tmux_has_session(name, host):
        raise HTTPException(status_code=404, detail=f"no live tmux session '{name}'")

    # We deliberately do NOT record a claude_session_id. A hand-started claude exposes
    # its id nowhere reliable — not in argv/env, and it append-closes its transcript so
    # an open-fd trace can't tie a pane to a file — and several claudes can share one
    # cwd, so any guess risks binding the WRONG conversation. So an adopted session
    # attaches (its live tmux) and drives its card's lane, but has no resume-after-death.
    # The summary, by contrast, comes straight off the pane's statusline (reliable) and
    # only titles the auto-created card.
    st = await term.session_status(name, host)
    summary = (st or {}).get("summary") or (st or {}).get("task")

    card_id = (payload.get("card_id") or "").strip()
    if card_id:
        if not await session.get(Card, card_id):
            raise HTTPException(status_code=404, detail="card not found")
    else:
        title = (payload.get("title") or "").strip() or summary or name
        card = await upsert_card(
            session,
            origin="manual",
            external_id=new_id(),
            title=title,
            summary="",
            cached_patch={"manual": {"open": True, "board": ""}},
        )
        card_id = card.id

    # one binding per (card, tmux, host): reuse an existing row so re-adopting is
    # idempotent. host is part of the identity — the same tmux NAME can exist
    # independently on two different machines.
    existing = (
        await session.execute(
            select(TerminalSession).where(
                TerminalSession.card_id == card_id,
                TerminalSession.tmux_session == name,
                TerminalSession.host == host,
            )
        )
    ).scalars().first()
    ts = existing or TerminalSession(card_id=card_id, created_at=utcnow())
    ts.kind = "adopted"
    ts.host = host
    ts.tmux_session = name
    ts.status = "live"
    if existing is None:
        session.add(ts)
        await session.flush()
    # adopting is an EXPLICIT ownership transfer — drop the same tmux's rows on other
    # cards ON THE SAME HOST, or the pane poll would drive several cards' state from
    # one pane. Scoped by host too: a same-named tmux on a different machine is a
    # different session entirely and must not cross-delete.
    others = (
        await session.execute(
            select(TerminalSession).where(
                TerminalSession.tmux_session == name,
                TerminalSession.host == host,
                TerminalSession.id != ts.id,
            )
        )
    ).scalars().all()
    for old in others:
        if old.card_id != card_id and old.claude_session_id:
            # the losing card must FORGET this conversation too — mirrors
            # open_terminal's ownership-transfer clear (PROJ-3783/PROJ-10367): its
            # LocalState.claude_session_id pointer outlives the row, so otherwise it
            # can still auto-resume a conversation this tmux no longer represents on
            # that card. Only when the pointer still names THIS conversation — a card
            # pointing elsewhere is left alone.
            other_ls = await session.get(LocalState, old.card_id)
            if other_ls and other_ls.claude_session_id == old.claude_session_id:
                other_ls.claude_session_id = None
                other_ls.host = None
        await session.delete(old)
    await session.commit()
    return {"ok": True, "card_id": card_id, "tmux_session": name, "host": host}


# ── handover: move a task between machines (Base ⇄ Roam) via a doc, not the transcript ──
# prompt templates live in prompts.py (user-overridable via ~/.conductor/prompts.json)


@router.post("/cards/{card_id}/handover/prep")
async def handover_prep(
    card_id: str, payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    """Inject the handover write-prompt into the card's active claude pane so it writes the
    handover doc + commits/pushes. Best used while claude is idle (send-keys types into its
    input); a busy claude would mangle it."""
    if not await session.get(Card, card_id):
        raise HTTPException(status_code=404, detail="card not found")
    name = (payload.get("tmux") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="no active session to write from")
    host = _norm_host(payload.get("host"))
    await term.clear_handover(card_id, host)  # so the caller's poll only sees a fresh doc
    await term.send_prompt(name, prompts.prompt("handover_write", cid=card_id), host)
    return {"ok": True}


@router.post("/cards/{card_id}/handover/transfer")
async def handover_transfer(
    card_id: str, payload: dict, session: AsyncSession = Depends(get_session)
) -> dict:
    """Copy the card's handover doc from `from_host` to `to_host` over the tailnet (never via
    the repo). Returns the pickup prompt to seed a fresh session on the target. The CODE
    itself travels via git (the write-prompt told claude to push); this only moves the doc."""
    if not await session.get(Card, card_id):
        raise HTTPException(status_code=404, detail="card not found")
    from_host = _norm_host(payload.get("from_host"))
    to_host = _norm_host(payload.get("to_host"))
    if from_host == to_host:
        raise HTTPException(status_code=400, detail="source and target are the same machine")
    if to_host and not await term.reachable(to_host):
        raise HTTPException(status_code=503, detail=f"host '{to_host}' is offline")
    # Guard the SOURCE too: if it's offline, read_handover fails silently and returns None,
    # which the caller would read as "not written yet" and re-inject — clobbering a doc that
    # is merely stranded on a sleeping machine. Fail loudly instead so the doc stays put.
    if from_host and not await term.reachable(from_host):
        raise HTTPException(
            status_code=503, detail=f"source host '{from_host}' is offline — bring it online, then hand off"
        )
    doc = await term.read_handover(card_id, from_host)
    if not doc:
        # not an error — claude hasn't finished writing yet; the caller polls on this
        return {"ok": True, "ready": False}
    await term.write_handover(card_id, doc, to_host)
    # Move, not copy: drop the source doc once it's safely on the target. This keeps a
    # later re-handover of the same card from picking up this now-consumed doc (the
    # resumable "is a doc already waiting?" check would otherwise reuse it forever).
    await term.clear_handover(card_id, from_host)
    return {
        "ok": True,
        "ready": True,
        "to_host": to_host,
        "bytes": len(doc),
        "pickup_prompt": prompts.prompt("handover_pickup", cid=card_id),
    }
