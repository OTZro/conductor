"""StateProbeSpec: plugins supply extra running/waiting evidence to the pane poll.

The seam exists for agent teams — a team lead idles at its prompt while its teammates
work, so every pane reads waiting even though the card is very much in flight. A probe
can only PROMOTE to running (never demote), a live choice dialog outranks it (claude is
literally asking the human), and a probe that raises abstains instead of breaking the
poll. Same fixture idiom as test_agent_poll: throwaway sqlite + monkeypatched tmux."""

from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from conductor import db as db_mod
from conductor import plugins as plugins_mod
from conductor.actions import agent_state
from conductor.actions import terminal as term
from conductor.models import Card, TerminalSession
from conductor.plugins import Plugin, StateProbeSpec


@pytest.fixture
async def maker(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/probe.db")
    m = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", m)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield m
    await engine.dispose()


async def _seed_card(maker) -> str:
    async with maker() as s:
        card = Card(origin="jira", external_id="PROJ-1", title="t", cached={})
        s.add(card)
        await s.flush()
        s.add(
            TerminalSession(
                card_id=card.id, kind="own", tmux_session="conductor-abc12345",
                host=None, status="live",
            )
        )
        await s.commit()
        return card.id


def _install(monkeypatch, probe) -> None:
    """Register a throwaway plugin carrying `probe` — the poll reads PLUGINS lazily,
    so an entry here is exactly what discovery would have produced."""
    monkeypatch.setitem(
        plugins_mod.PLUGINS,
        "probe-test",
        Plugin(id="probe-test", label="P", icon="?", state_probes=(StateProbeSpec(probe),)),
    )


def _fake_tmux(monkeypatch, classify_result):
    async def fake_list_tmux():
        return [{"name": "conductor-abc12345", "kind": "conductor", "attached": False, "activity": 0, "host": None}]

    async def fake_classify(name, host=None, sustain_running=False):
        return classify_result

    monkeypatch.setattr(term, "list_tmux", fake_list_tmux)
    monkeypatch.setattr(agent_state, "_classify_pane", fake_classify)


async def test_probe_promotes_idle_pane_to_running(maker, monkeypatch):
    cid = await _seed_card(maker)
    _fake_tmux(monkeypatch, ("waiting", None, None))

    seen_ctx: dict = {}

    async def probe(ctx):
        seen_ctx.update(ctx)
        return ("running", "team 3/8 tasks")

    _install(monkeypatch, probe)
    await agent_state.poll_agent_states()

    async with maker() as s:
        c = await s.get(Card, cid)
    agent = (c.cached or {}).get("agent") or {}
    assert agent.get("running") is True
    assert agent.get("bg") == "team 3/8 tasks"  # the probe's label reaches the card
    assert c.ball == "ai"
    # the probe got the session's identity to reason about
    assert seen_ctx["name"] == "conductor-abc12345"
    assert seen_ctx["card_id"] == cid


async def test_choice_dialog_outranks_probe(maker, monkeypatch):
    cid = await _seed_card(maker)
    _fake_tmux(monkeypatch, ("waiting", None, "Do you want to proceed?"))

    called = False

    async def probe(ctx):
        nonlocal called
        called = True
        return ("running", None)

    _install(monkeypatch, probe)
    await agent_state.poll_agent_states()

    async with maker() as s:
        c = await s.get(Card, cid)
    agent = (c.cached or {}).get("agent") or {}
    assert called is False  # never consulted — claude is asking the human
    assert agent.get("waiting") is True
    assert agent.get("choice") == "Do you want to proceed?"


async def test_broken_probe_abstains(maker, monkeypatch):
    cid = await _seed_card(maker)
    _fake_tmux(monkeypatch, ("waiting", None, None))

    async def probe(ctx):
        raise RuntimeError("plugin bug")

    _install(monkeypatch, probe)
    await agent_state.poll_agent_states()  # must not raise

    async with maker() as s:
        c = await s.get(Card, cid)
    agent = (c.cached or {}).get("agent") or {}
    assert agent.get("running") is not True  # abstained → pane verdict stands


async def test_running_pane_skips_probes(maker, monkeypatch):
    await _seed_card(maker)
    _fake_tmux(monkeypatch, ("running", None, None))

    called = False

    async def probe(ctx):
        nonlocal called
        called = True
        return None

    _install(monkeypatch, probe)
    await agent_state.poll_agent_states()
    assert called is False  # panes already prove running — no probe cost


async def test_probes_run_concurrently_under_one_card_deadline(monkeypatch):
    """A hung probe must cost at most the single 2s card deadline AND must not mask a
    fast probe's verdict — probes run concurrently, not serially (the 2s × specs ×
    sessions pile-up class)."""

    async def hung(ctx):  # never finishes inside the deadline
        await asyncio.sleep(30)

    async def fast(ctx):
        return ("running", "team 3/8 tasks")

    monkeypatch.setitem(
        plugins_mod.PLUGINS, "zz-hung",
        Plugin(id="zz-hung", label="H", icon="?", state_probes=(StateProbeSpec(hung),)),
    )
    monkeypatch.setitem(
        plugins_mod.PLUGINS, "zz-fast",
        Plugin(id="zz-fast", label="F", icon="?", state_probes=(StateProbeSpec(fast),)),
    )
    t0 = time.monotonic()
    got = await agent_state._probe_state("card-x", {("s1", None), ("s2", None)}, False)
    elapsed = time.monotonic() - t0
    assert got == ("running", "team 3/8 tasks")  # fast verdict not masked by the hung one
    assert elapsed < 3.5  # ONE card deadline — not 2s × probes × sessions
