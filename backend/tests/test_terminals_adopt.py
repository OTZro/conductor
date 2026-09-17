"""Terminals API: the ?status=1 claude gate for card-less sessions, and adopting a
hand-run claude onto a card.

Driven through httpx.ASGITransport with the db engine swapped to a throwaway sqlite
file (same pattern as test_auth) — the tmux/pane/ssh calls are monkeypatched, so no
real tmux or remote host is needed."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from conductor import db as db_mod
from conductor.actions import terminal as term
from conductor.main import app
from conductor.models import Card, LocalState, TerminalSession


@pytest.fixture
async def client(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/term-test.db")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", maker)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    await engine.dispose()


def _sess(name: str, kind: str, host: str | None = None) -> dict:
    return {"name": name, "kind": kind, "attached": False, "activity": 0, "host": host}


# --- ?status=1 gate: 'other' sessions show status only when the pane is claude ---


async def test_status_gate_surfaces_claude_but_not_plain_shells(client, monkeypatch):
    sessions = [
        _sess("conductor-abc12345", "conductor"),
        _sess("shell-claude1", "other"),
        _sess("shell-bash01", "other"),
    ]
    statuses = {
        "conductor-abc12345": {"state": "waiting", "is_claude": True, "model": "Opus 4.8"},
        "shell-claude1": {"state": "running", "is_claude": True, "model": "Fable 5"},
        "shell-bash01": {"state": "waiting", "is_claude": False, "model": None},
    }

    async def fake_list_tmux():
        return [dict(s) for s in sessions]

    async def fake_status(name, host=None, ttl=6.0):
        return statuses.get(name)

    monkeypatch.setattr(term, "list_tmux", fake_list_tmux)
    monkeypatch.setattr(term, "session_status", fake_status)

    r = await client.get("/api/tmux?status=1")
    assert r.status_code == 200
    by_name = {s["name"]: s for s in r.json()}
    # known-claude conductor session: status attached
    assert by_name["conductor-abc12345"]["status"]["model"] == "Opus 4.8"
    # card-less shell running claude: status attached (is_claude True)
    assert by_name["shell-claude1"]["status"]["state"] == "running"
    # card-less plain shell: NO status (is_claude False) → no fake idle dot
    assert "status" not in by_name["shell-bash01"]


# --- adopt: bind a card-less claude session onto a card ---


def _patch_tmux(monkeypatch, *, summary="fix the thing"):
    async def has_session(name, host=None):
        return True

    async def status(name, host=None, ttl=6.0):
        return {"state": "waiting", "is_claude": True, "summary": summary}

    monkeypatch.setattr(term, "_tmux_has_session", has_session)
    monkeypatch.setattr(term, "session_status", status)


async def test_adopt_creates_manual_card_and_binds(client, monkeypatch):
    _patch_tmux(monkeypatch, summary="ship adopt")
    r = await client.post("/api/tmux/adopt", json={"name": "shell-5e1003"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["tmux_session"] == "shell-5e1003"
    cid = body["card_id"]

    async with db_mod.session_maker() as s:
        card = await s.get(Card, cid)
        assert card is not None and card.origin == "manual"
        assert card.title == "ship adopt"  # titled from the pane's claude summary
        assert (card.cached or {}).get("manual", {}).get("open") is True

        rows = (
            await s.execute(select(TerminalSession).where(TerminalSession.card_id == cid))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].kind == "adopted"
        assert rows[0].tmux_session == "shell-5e1003"
        assert rows[0].status == "live"
        # no sid recorded — a hand-run claude's id can't be tied to its pane reliably
        assert rows[0].claude_session_id is None


async def test_adopt_onto_existing_card_is_idempotent(client, monkeypatch):
    _patch_tmux(monkeypatch)
    async with db_mod.session_maker() as s:
        card = Card(origin="jira", external_id="PROJ-9", title="a ticket")
        s.add(card)
        await s.commit()
        cid = card.id

    # adopt twice onto the same card → exactly one binding row (re-adopt is a no-op upsert)
    for _ in range(2):
        r = await client.post("/api/tmux/adopt", json={"name": "shell-x", "card_id": cid})
        assert r.status_code == 200 and r.json()["card_id"] == cid

    async with db_mod.session_maker() as s:
        rows = (
            await s.execute(select(TerminalSession).where(TerminalSession.card_id == cid))
        ).scalars().all()
        assert len(rows) == 1 and rows[0].tmux_session == "shell-x"


async def test_adopt_unknown_card_404(client, monkeypatch):
    _patch_tmux(monkeypatch)
    r = await client.post("/api/tmux/adopt", json={"name": "shell-x", "card_id": "nope"})
    assert r.status_code == 404


async def test_adopt_transfers_ownership_across_cards(client, monkeypatch):
    """Adopting a tmux onto card B removes card A's binding for the same tmux — the
    pane poll maps a tmux to EVERY card holding a row, so a shared binding made one
    running pane drive several cards' agent state (the PROJ-3783/PROJ-10367 case)."""
    _patch_tmux(monkeypatch)
    async with db_mod.session_maker() as s:
        a = Card(origin="jira", external_id="ACE-A", title="a")
        b = Card(origin="jira", external_id="ACE-B", title="b")
        s.add(a)
        s.add(b)
        await s.commit()
        aid, bid = a.id, b.id

    assert (await client.post("/api/tmux/adopt", json={"name": "shell-x", "card_id": aid})).status_code == 200
    assert (await client.post("/api/tmux/adopt", json={"name": "shell-x", "card_id": bid})).status_code == 200

    async with db_mod.session_maker() as s:
        rows = (
            await s.execute(
                select(TerminalSession).where(TerminalSession.tmux_session == "shell-x")
            )
        ).scalars().all()
    assert len(rows) == 1 and rows[0].card_id == bid  # B owns it now; A's row is gone


async def test_adopt_same_tmux_name_different_hosts_does_not_cross_delete(client, monkeypatch):
    """Two different machines can each have a live tmux session that happens to share a
    name — host is part of the binding's identity, so adopting the LOCAL one onto card A
    and the REMOTE one (same name) onto card B must not delete each other's row."""
    monkeypatch.setattr(term.settings, "remote_hosts", "roam=example.ts.net")
    _patch_tmux(monkeypatch)

    async def fake_reachable(host):
        return True

    monkeypatch.setattr(term, "reachable", fake_reachable)

    async with db_mod.session_maker() as s:
        a = Card(origin="jira", external_id="ACE-HOST-A", title="a")
        b = Card(origin="jira", external_id="ACE-HOST-B", title="b")
        s.add(a)
        s.add(b)
        await s.commit()
        aid, bid = a.id, b.id

    r1 = await client.post("/api/tmux/adopt", json={"name": "shell-x", "card_id": aid})
    assert r1.status_code == 200 and r1.json()["host"] is None
    r2 = await client.post(
        "/api/tmux/adopt", json={"name": "shell-x", "card_id": bid, "host": "roam"}
    )
    assert r2.status_code == 200 and r2.json()["host"] == "roam"

    async with db_mod.session_maker() as s:
        rows = (
            await s.execute(
                select(TerminalSession).where(TerminalSession.tmux_session == "shell-x")
            )
        ).scalars().all()
    # exactly two rows survive — assert before building the dict, which would otherwise
    # mask a stray duplicate binding for a card
    assert len(rows) == 2
    by_card = {row.card_id: row for row in rows}
    # BOTH survive — same tmux name, different hosts, not the same binding
    assert aid in by_card and bid in by_card
    assert by_card[aid].host is None
    assert by_card[bid].host == "roam"


async def test_adopt_transfer_clears_losing_cards_stale_local_state(client, monkeypatch):
    """Card A previously had a real claude conversation bound to this tmux (e.g. from an
    earlier own/resume open) and its LocalState still remembers it. When card B adopts
    the same tmux away from A, A's LocalState pointer must be cleared too — otherwise A's
    next auto-open tries to re-resume a conversation it no longer owns (the
    PROJ-3783/PROJ-10367 recurrence this PR fixes for open_terminal; adopt needed the same
    guard)."""
    SID = "conv-adopt-aaaa-bbbb-cccc"
    _patch_tmux(monkeypatch)

    async with db_mod.session_maker() as s:
        a = Card(origin="jira", external_id="ACE-ADOPT-A", title="a")
        b = Card(origin="jira", external_id="ACE-ADOPT-B", title="b")
        s.add(a)
        s.add(b)
        await s.flush()
        s.add(TerminalSession(card_id=a.id, kind="own", tmux_session="shell-adopt-x",
                              claude_session_id=SID, status="live"))
        s.add(LocalState(card_id=a.id, claude_session_id=SID))
        await s.commit()
        aid, bid = a.id, b.id

    r = await client.post("/api/tmux/adopt", json={"name": "shell-adopt-x", "card_id": bid})
    assert r.status_code == 200

    async with db_mod.session_maker() as s:
        rows = (
            await s.execute(
                select(TerminalSession).where(TerminalSession.tmux_session == "shell-adopt-x")
            )
        ).scalars().all()
        assert len(rows) == 1 and rows[0].card_id == bid  # B owns it now, A's row is gone

        a_ls = await s.get(LocalState, aid)
        assert a_ls.claude_session_id is None  # A forgot the conversation → won't re-resume
        assert a_ls.host is None


async def test_own_terminal_non_string_profile_is_clean_400(client, monkeypatch, tmp_path):
    """A direct API call with a non-string `profile` must be a clean 400 (the field is
    string-coerced → unknown-profile lookup), never a 500 from `.strip()` on a non-str."""
    monkeypatch.setattr(term.settings, "launch_profiles_file", tmp_path / "none.json")  # → no profiles
    async with db_mod.session_maker() as s:
        card = Card(origin="manual", external_id="p1", title="t")
        s.add(card)
        await s.commit()
        cid = card.id
    # every non-string value (truthy AND falsy) is a clean 400, not silently ignored
    for bad in (123, False, 0, [], {}):
        r = await client.post(f"/api/cards/{cid}/terminal", json={"kind": "own", "profile": bad})
        assert r.status_code == 400, bad
        assert "profile" in r.text.lower()


async def test_own_terminal_rejects_non_string_host(client):
    """A non-string `host` must 400 (it hits _norm_host().strip()), not 500."""
    async with db_mod.session_maker() as s:
        card = Card(origin="manual", external_id="h1", title="t")
        s.add(card)
        await s.commit()
        cid = card.id
    r = await client.post(f"/api/cards/{cid}/terminal", json={"kind": "own", "host": 123})
    assert r.status_code == 400
    assert "host" in r.text.lower()


async def test_own_terminal_rejects_non_boolean_attach_only(client):
    """`attach_only` must be a real JSON bool — a stringified 'false' must 400, not be
    coerced to True (`bool('false')` is True, which would silently disable revival)."""
    async with db_mod.session_maker() as s:
        card = Card(origin="manual", external_id="ao1", title="t")
        s.add(card)
        await s.commit()
        cid = card.id
    r = await client.post(f"/api/cards/{cid}/terminal", json={"kind": "own", "attach_only": "false"})
    assert r.status_code == 400
    assert "attach_only" in r.text.lower()


async def test_adopt_dead_session_404(client, monkeypatch):
    async def no_session(name, host=None):
        return False

    monkeypatch.setattr(term, "_tmux_has_session", no_session)
    r = await client.post("/api/tmux/adopt", json={"name": "shell-ghost"})
    assert r.status_code == 404


async def test_adopt_requires_name(client):
    r = await client.post("/api/tmux/adopt", json={})
    assert r.status_code == 400


async def test_explicit_resume_transfers_ownership_and_clears_losing_pointer(client, monkeypatch):
    """A conversation belongs to ONE card. When card B explicitly resumes conversation
    X that card A used, A loses BOTH its row AND its LocalState pointer — otherwise A's
    next auto-open re-resumes X and re-binds it (the PROJ-3783/PROJ-10367 recurrence)."""
    from conductor.models import LocalState

    SID = "conv-x-1111-2222-3333"

    async def fake_open(**kw):
        return {"port": 1, "url": f"/term/{kw['session_id']}/", "tmux_session": "conductor-convx",
                "pid": 7, "cwd": "/tmp", "claude_session_id": SID, "host": None}

    monkeypatch.setattr(term, "open_terminal", fake_open)

    async with db_mod.session_maker() as s:
        a = Card(origin="jira", external_id="ACE-A", title="a")
        b = Card(origin="jira", external_id="ACE-B", title="b")
        s.add(a)
        s.add(b)
        await s.flush()
        # A already owns conversation X: a live row + its remembered pointer
        s.add(TerminalSession(card_id=a.id, kind="resume", tmux_session="conductor-convx",
                              claude_session_id=SID, status="live"))
        s.add(LocalState(card_id=a.id, claude_session_id=SID))
        await s.commit()
        aid, bid = a.id, b.id

    # B explicitly resumes X (not attach_only)
    r = await client.post(f"/api/cards/{bid}/terminal",
                          json={"kind": "resume", "claude_session_id": SID, "attach_only": False})
    assert r.status_code == 200

    async with db_mod.session_maker() as s:
        rows = (await s.execute(select(TerminalSession).where(
            TerminalSession.claude_session_id == SID, TerminalSession.status == "live"))).scalars().all()
        assert len(rows) == 1 and rows[0].card_id == bid  # B owns the single live row
        a_ls = await s.get(LocalState, aid)
        assert a_ls.claude_session_id is None  # A forgot X → won't re-resume on view
        b_ls = await s.get(LocalState, bid)
        assert b_ls.claude_session_id == SID  # B remembers it
