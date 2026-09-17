"""Fork plugin: confirmation parsing, session selection, endpoint guards, and the
full flow with the tmux/claude layer mocked (the real /fork interaction was verified
live against claude 2026-07-22; these pin the plumbing around it). sqlite-backed."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from conductor import db as db_mod
from conductor.actions import agent_state
from conductor.actions import terminal as term
from conductor.config import settings
from conductor.main import app
from conductor.models import TerminalSession, utcnow
from conductor.plugins.fork import _card_data as fork_widget
from conductor.plugins.fork import router as fork_router

_PANE = """
❯ say hi in two words
⏺ Hi there!
❯ /fork
  ⎿  session waiting for a prompt · Say hi in two words · c51d1118
"""


def test_fork_confirmation_parse():
    assert fork_router._FORK_ID_RE.findall(_PANE)[-1] == "c51d1118"
    assert fork_router._FORK_ID_RE.findall("no confirmation here") == []


@pytest.fixture
async def client(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/fork-test.db")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", maker)
    monkeypatch.setattr(settings, "auth_enabled", False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c
    await engine.dispose()


async def _card_with_session(client, *, claude_sid="aaaa1111-0000-0000-0000-000000000000") -> str:
    cid = (await client.post("/api/cards", json={"title": "fork test"})).json()["id"]
    async with db_mod.session_maker() as s:
        s.add(TerminalSession(
            card_id=cid, kind="own", tmux_session="conductor-aaaa1111", host=None,
            status="live", claude_session_id=claude_sid, created_at=utcnow(),
        ))
        await s.commit()
    return cid


async def test_widget_offers_only_with_forkable_session(client):
    bare = (await client.post("/api/cards", json={"title": "no session"})).json()["id"]
    got = (await client.get(f"/api/cards/{bare}")).json()
    ctx = {"origin": got["origin"], "external_id": got["external_id"], "links": []}
    assert await fork_widget(ctx) is None  # nothing to fork → no menu row
    cid = await _card_with_session(client)
    got = (await client.get(f"/api/cards/{cid}")).json()
    ctx = {"origin": got["origin"], "external_id": got["external_id"], "links": []}
    assert (await fork_widget(ctx)) == {"card_id": cid}


async def test_fork_404_unknown_card_and_409_without_session(client):
    assert (await client.post("/api/plugins/fork/nope")).status_code == 404
    bare = (await client.post("/api/cards", json={"title": "bare"})).json()["id"]
    assert (await client.post(f"/api/plugins/fork/{bare}")).status_code == 409


async def test_fork_409_when_tmux_dead_or_mid_turn(client, monkeypatch):
    cid = await _card_with_session(client)

    async def dead(name, host=None):
        return False

    monkeypatch.setattr(term, "_tmux_has_session", dead)
    r = await client.post(f"/api/plugins/fork/{cid}")
    assert r.status_code == 409 and "not running" in r.json()["detail"]

    async def alive(name, host=None):
        return True

    async def running(name, host=None, sustain_running=False):
        return ("running", None, None)

    monkeypatch.setattr(term, "_tmux_has_session", alive)
    monkeypatch.setattr(agent_state, "_classify_pane", running)
    r = await client.post(f"/api/plugins/fork/{cid}")
    assert r.status_code == 409 and "mid-turn" in r.json()["detail"]


async def test_fork_surfaces_claudes_own_refusal(client, monkeypatch):
    """An empty conversation can't fork — claude prints '⎿ Nothing to fork yet…'.
    That exact reason must reach the caller as a 409, not a generic 502."""
    cid = await _card_with_session(client)
    refusal = "❯ /fork\n  ⎿  Nothing to fork yet. Send a message first.\n❯\n"

    async def alive(name, host=None):
        return True

    async def waiting(name, host=None, sustain_running=False):
        return ("waiting", None, None)

    async def fake_run(argv, host=None, input_=None, timeout=8):
        if argv[:2] == ["tmux", "capture-pane"]:
            return 0, refusal.encode()
        return 0, b""

    monkeypatch.setattr(term, "_tmux_has_session", alive)
    monkeypatch.setattr(agent_state, "_classify_pane", waiting)
    monkeypatch.setattr(fork_router, "_run", fake_run)
    r = await client.post(f"/api/plugins/fork/{cid}")
    assert r.status_code == 409 and "Nothing to fork yet" in r.json()["detail"]


async def test_fork_full_flow_with_mocked_pane(client, monkeypatch):
    """send-keys → confirmation parse → full-sid resolve → second window opened with
    the FORKED conversation and persisted as a live TerminalSession row."""
    cid = await _card_with_session(client)
    sent: list[list[str]] = []
    full_sid = "c51d1118-010d-4276-b9d1-8a594c7cd444"

    async def alive(name, host=None):
        return True

    async def waiting(name, host=None, sustain_running=False):
        return ("waiting", None, None)

    async def fake_run(argv, host=None, input_=None, timeout=8):
        sent.append(argv)
        if argv[:2] == ["tmux", "send-keys"]:
            return 0, b""
        if argv[:2] == ["tmux", "capture-pane"]:
            return 0, _PANE.encode()
        if argv[0] == "sh":  # the find that resolves the full sid
            return 0, f"/x/.claude/projects/-tmp/{full_sid}.jsonl\n".encode()
        return 0, b""

    async def fake_open_terminal(**kw):
        assert kw["kind"] == "resume" and kw["claude_session_id"] == full_sid
        return {"port": 1, "url": f"/term/{kw['session_id']}/", "tmux_session": "conductor-c51d1118",
                "pid": 42, "cwd": "/tmp", "claude_session_id": full_sid, "host": None}

    monkeypatch.setattr(term, "_tmux_has_session", alive)
    monkeypatch.setattr(agent_state, "_classify_pane", waiting)
    monkeypatch.setattr(fork_router, "_run", fake_run)
    monkeypatch.setattr(term, "open_terminal", fake_open_terminal)

    r = await client.post(f"/api/plugins/fork/{cid}")
    assert r.status_code == 200
    body = r.json()
    assert body["fork"] == "c51d1118" and body["claude_session_id"] == full_sid
    # the /fork keystrokes actually went to the pane
    keys = [a for a in sent if a[:2] == ["tmux", "send-keys"]]
    assert ["tmux", "send-keys", "-t", "conductor-aaaa1111", "/fork"] == keys[0]
    assert keys[1][-1] == "Enter"
    # window 2 persisted live on the forked conversation
    async with db_mod.session_maker() as s:
        rows = (await s.execute(
            select(TerminalSession).where(TerminalSession.card_id == cid)
        )).scalars().all()
    forked = [x for x in rows if x.claude_session_id == full_sid]
    assert forked and forked[0].status == "live" and forked[0].kind == "resume"
