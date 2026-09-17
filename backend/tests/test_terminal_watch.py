"""API-level coverage for the generalized read-only Watch (see actions/terminal.py's
`watch` param and api/terminals.py's cached.watch extraction): a card whose
`cached.watch` names a live tmux (e.g. an agent member's claude pane) attaches to
THAT via kind="attach" — the only mechanism attach has; there is no session
convention to fall back to. Same ASGITransport + throwaway-sqlite pattern as
test_terminals_adopt.py — tmux/ttyd calls are monkeypatched, no real tmux or
subprocess is spawned."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from conductor import db as db_mod
from conductor.actions import terminal as term
from conductor.main import app
from conductor.models import Card, TerminalSession


@pytest.fixture
async def client(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/watch-test.db")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", maker)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    await engine.dispose()


class _FakeProc:
    pid = 4242
    returncode = None

    def terminate(self):
        pass


def _mock_ttyd(monkeypatch, *, has_session=True):
    """Stub out real tmux/ttyd spawning: has-session says whether the target lives;
    ttyd's subprocess is faked so no real process is spawned in tests."""

    async def fake_has_session(name, host=None, socket=None):
        return has_session

    async def fake_create_subprocess_exec(*args, **kw):
        return _FakeProc()

    monkeypatch.setattr(term, "_tmux_has_session", fake_has_session)
    monkeypatch.setattr(term, "_free_port", lambda start: 17500)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)


async def test_attach_uses_cached_watch_when_present(client, monkeypatch):
    """cached.watch on a card resolves through kind='attach' to the WATCH target."""
    _mock_ttyd(monkeypatch)
    async with db_mod.session_maker() as s:
        card = Card(
            origin="agents",
            external_id="task:1",
            title="t",
            cached={"watch": {"socket": "agents", "session": "member-alice", "host": None}},
        )
        s.add(card)
        await s.commit()
        cid = card.id

    r = await client.post(f"/api/cards/{cid}/terminal", json={"kind": "attach"})
    assert r.status_code == 200
    body = r.json()
    assert body["claude_session_id"] is None  # attach never resumes/creates a conversation

    async with db_mod.session_maker() as s:
        ts = await s.get(TerminalSession, body["id"])
        assert ts.tmux_session == "member-alice"  # attached to the declared WATCH target
        assert ts.status == "live"


async def test_attach_requires_cached_watch(client, monkeypatch):
    """A card with NO cached.watch has nothing for attach to bind to — there is no
    session-naming convention to fall back to, so the request must fail cleanly."""
    _mock_ttyd(monkeypatch)
    async with db_mod.session_maker() as s:
        card = Card(
            origin="jira",
            external_id="PROJ-1",
            title="t",
            cached={},
        )
        s.add(card)
        await s.commit()
        cid = card.id

    r = await client.post(f"/api/cards/{cid}/terminal", json={"kind": "attach"})
    assert r.status_code == 400


async def test_attach_fails_cleanly_when_watch_target_not_live(client, monkeypatch):
    """'attach a RUNNING session or fail, never revive' holds for the generic watch
    path too — a dead target is a clean 400, not a spawned-then-broken pane."""
    _mock_ttyd(monkeypatch, has_session=False)
    async with db_mod.session_maker() as s:
        card = Card(
            origin="agents",
            external_id="task:2",
            title="t",
            cached={"watch": {"socket": "agents", "session": "member-gone", "host": None}},
        )
        s.add(card)
        await s.commit()
        cid = card.id

    r = await client.post(f"/api/cards/{cid}/terminal", json={"kind": "attach"})
    assert r.status_code == 400
    assert "member-gone" in r.text
