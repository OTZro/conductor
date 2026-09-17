"""poll_agent_states: any RUNNING claude pane bound to a card drives it to AI Working,
and a card can be bound to more than one live pane at once (e.g. two adopted sessions,
or one pane shared by two cards) — the poll must read every bound pane concurrently and
combine their verdicts correctly rather than assuming exactly one.

Same fixture idiom as test_terminals_adopt: db engine swapped to a throwaway sqlite,
tmux/pane calls monkeypatched (no real tmux needed)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from conductor import db as db_mod
from conductor.actions import agent_state
from conductor.actions import terminal as term
from conductor.models import Card, TerminalSession


@pytest.fixture
async def maker(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/poll.db")
    m = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", m)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield m
    await engine.dispose()


def _sess(name: str, kind: str, host: str | None = None) -> dict:
    return {"name": name, "kind": kind, "attached": False, "activity": 0, "host": host}


async def _seed_card(maker, *, conductor_session: bool = True, second_session: bool = False) -> str:
    """A plain card, optionally with one or two live conductor sessions bound to it
    (`second_session` is for tests exercising concurrent multi-pane capture)."""
    async with maker() as s:
        card = Card(origin="jira", external_id="PROJ-1", title="t", cached={})
        s.add(card)
        await s.flush()
        if conductor_session:
            s.add(
                TerminalSession(
                    card_id=card.id, kind="own", tmux_session="conductor-abc12345",
                    host=None, status="live",
                )
            )
        if second_session:
            s.add(
                TerminalSession(
                    card_id=card.id, kind="own", tmux_session="conductor-def67890",
                    host=None, status="live",
                )
            )
        await s.commit()
        return card.id


async def test_panes_are_captured_concurrently(maker, monkeypatch):
    """The captures must overlap, not queue. Each one is a tmux subprocess locally and a
    full ssh round trip for a remote session; serialized inside a 5s poll they used to
    cost the SUM of every pane. Asserted by observing peak in-flight depth rather than
    wall clock, so it can't go flaky on a loaded machine."""
    import asyncio

    await _seed_card(maker, conductor_session=True, second_session=True)

    async def fake_list_tmux():
        return [
            _sess("conductor-abc12345", "conductor"),
            _sess("conductor-def67890", "conductor"),
        ]

    depth = peak = 0

    async def fake_classify(name, host=None, sustain_running=False):
        nonlocal depth, peak
        depth += 1
        peak = max(peak, depth)
        await asyncio.sleep(0)  # yield: a serial implementation never overlaps here
        depth -= 1
        return ("waiting", None, None)

    monkeypatch.setattr(term, "list_tmux", fake_list_tmux)
    monkeypatch.setattr(agent_state, "_classify_pane", fake_classify)

    await agent_state.poll_agent_states()

    assert peak == 2, f"panes were captured {peak} at a time — expected both in flight"


async def test_one_unreadable_pane_does_not_sink_the_cycle(maker, monkeypatch):
    """gather(return_exceptions=True): a pane that raises is dropped, and the card is
    still decided from whatever else was readable. Without it a single broken ssh would
    abort the whole poll and freeze every card's lane."""
    cid = await _seed_card(maker, conductor_session=True, second_session=True)

    async def fake_list_tmux():
        return [
            _sess("conductor-abc12345", "conductor"),
            _sess("conductor-def67890", "conductor"),
        ]

    async def fake_classify(name, host=None, sustain_running=False):
        if name == "conductor-abc12345":
            raise OSError("ssh died")
        return ("running", None, None)

    monkeypatch.setattr(term, "list_tmux", fake_list_tmux)
    monkeypatch.setattr(agent_state, "_classify_pane", fake_classify)

    await agent_state.poll_agent_states()

    async with maker() as s:
        c = await s.get(Card, cid)
    assert ((c.cached or {}).get("agent") or {}).get("running") is True
    assert c.ball == "ai"


async def test_idle_conductor_alone_is_not_promoted(maker, monkeypatch):
    # only the idle peek session is live; the pane-driven promotion must not fire
    # off anything but what the pane itself currently shows.
    cid = await _seed_card(maker, conductor_session=True)

    async def fake_list_tmux():
        return [_sess("conductor-abc12345", "conductor")]

    async def fake_classify(name, host=None, sustain_running=False):
        return ("waiting", None, None)

    monkeypatch.setattr(term, "list_tmux", fake_list_tmux)
    monkeypatch.setattr(agent_state, "_classify_pane", fake_classify)

    await agent_state.poll_agent_states()

    async with maker() as s:
        c = await s.get(Card, cid)
    agent = (c.cached or {}).get("agent") or {}
    assert agent.get("running") is not True
    assert c.ball != "ai"


async def _seed_pair_sharing_one_pane(maker) -> tuple[str, str]:
    """Two cards bound to the SAME tmux pane — what a PASSIVE attach_only open leaves
    behind: api/terminals.py deliberately does not steal the other card's row. Card A
    is already running, card B is not."""
    async with maker() as s:
        a = Card(origin="jira", external_id="PROJ-A", title="a",
                 cached={"agent": {"active": True, "running": True}})
        b = Card(origin="jira", external_id="PROJ-B", title="b", cached={})
        s.add(a)
        s.add(b)
        await s.flush()
        for card in (a, b):
            s.add(
                TerminalSession(
                    card_id=card.id, kind="own", tmux_session="conductor-shared1",
                    host=None, status="live",
                )
            )
        await s.commit()
        return a.id, b.id


async def test_a_shared_pane_is_captured_once_and_fanned_out(maker, monkeypatch):
    """One pane bound to two cards must be READ ONCE. Captured twice in the same gather
    they hash identical, so whichever coroutine resumes second reads `_PANE_SEEN`
    (keyed by (host, name)) back as prev == cur, loses the sustain_running bridge, and
    demotes a mid-turn claude to Need Human on the states the regexes miss. It also
    halves the captures."""
    a_id, b_id = await _seed_pair_sharing_one_pane(maker)

    async def fake_list_tmux():
        return [_sess("conductor-shared1", "conductor")]

    calls: list[tuple[str, str | None, bool]] = []

    async def fake_classify(name, host=None, sustain_running=False):
        calls.append((name, host, sustain_running))
        return ("running", None, None)

    monkeypatch.setattr(term, "list_tmux", fake_list_tmux)
    monkeypatch.setattr(agent_state, "_classify_pane", fake_classify)

    await agent_state.poll_agent_states()

    assert len(calls) == 1, f"the shared pane was captured {len(calls)} times: {calls}"
    # sustain is the UNION over the cards holding the pane: card A was running, so the
    # single capture must still get the bridge card B alone would not ask for.
    assert calls[0][2] is True
    async with maker() as s:
        a, b = await s.get(Card, a_id), await s.get(Card, b_id)
    for c in (a, b):  # the one result reaches BOTH cards
        assert ((c.cached or {}).get("agent") or {}).get("running") is True


async def test_captures_are_capped_per_host(maker, monkeypatch):
    """Unbounded fan-out spawns a tmux/ssh process per pane at once (runner._run has no
    limit of its own), so a large session set can exhaust processes, fds or the remote's
    MaxSessions — and the captures that die that way leave their cards on a stale lane."""
    import asyncio

    await _seed_card(maker, conductor_session=True, second_session=True)
    monkeypatch.setattr(agent_state.settings, "agent_poll_max_concurrent", 1)

    async def fake_list_tmux():
        return [
            _sess("conductor-abc12345", "conductor"),
            _sess("conductor-def67890", "conductor"),
        ]

    depth = peak = 0

    async def fake_classify(name, host=None, sustain_running=False):
        nonlocal depth, peak
        depth += 1
        peak = max(peak, depth)
        await asyncio.sleep(0)
        depth -= 1
        return ("waiting", None, None)

    monkeypatch.setattr(term, "list_tmux", fake_list_tmux)
    monkeypatch.setattr(agent_state, "_classify_pane", fake_classify)

    await agent_state.poll_agent_states()

    assert peak == 1, f"cap of 1 still ran {peak} captures at once"


async def test_a_failing_capture_reddens_the_health_dot(maker, monkeypatch):
    """A contained capture exception is still a health signal. `_run` raises (rather than
    returning non-zero) for a renamed tmux_bin or a broken ssh, and _run_poller's
    record_ok fires the moment this poll returns — so without its own /api/status entry a
    board where EVERY pane read is dying reports a green agent-state dot."""
    from conductor import status

    await _seed_card(maker, conductor_session=True)

    async def fake_list_tmux():
        return [_sess("conductor-abc12345", "conductor")]

    async def fake_classify(name, host=None, sustain_running=False):
        raise OSError("tmux: command not found")

    monkeypatch.setattr(term, "list_tmux", fake_list_tmux)
    monkeypatch.setattr(agent_state, "_classify_pane", fake_classify)

    await agent_state.poll_agent_states()
    err = status.snapshot()["agent-state-panes"]["error"]
    assert err and "1/1" in err and "tmux: command not found" in err

    # and it clears again once the panes read cleanly
    async def ok_classify(name, host=None, sustain_running=False):
        return ("waiting", None, None)

    monkeypatch.setattr(agent_state, "_classify_pane", ok_classify)
    await agent_state.poll_agent_states()
    assert status.snapshot()["agent-state-panes"]["error"] is None


async def test_probes_do_not_stale_each_other_s_snapshots(maker, monkeypatch):
    """`_probe_state` waits up to 2s per card. Awaited one at a time inside the decision
    loop, a later card's already-taken snapshot aged behind every earlier card's probe
    (2s × probing cards) and could be applied after it had gone out of date. Run
    together, the whole decision pass costs one 2s window and every card is decided from
    text of the same age."""
    import asyncio

    async with maker() as s:
        for n in ("ACE-P1", "ACE-P2"):
            card = Card(origin="jira", external_id=n, title=n, cached={})
            s.add(card)
            await s.flush()
            s.add(
                TerminalSession(
                    card_id=card.id, kind="own", tmux_session=f"conductor-{n.lower()}",
                    host=None, status="live",
                )
            )
        await s.commit()

    async def fake_list_tmux():
        return [_sess("conductor-ace-p1", "conductor"), _sess("conductor-ace-p2", "conductor")]

    async def fake_classify(name, host=None, sustain_running=False):
        return ("waiting", None, None)  # quiet + no dialog → both cards get probed

    depth = peak = 0

    async def fake_probe(card_id, sessions, was_running):
        nonlocal depth, peak
        depth += 1
        peak = max(peak, depth)
        await asyncio.sleep(0)  # a serial await per card never overlaps here
        depth -= 1
        return None

    monkeypatch.setattr(term, "list_tmux", fake_list_tmux)
    monkeypatch.setattr(agent_state, "_classify_pane", fake_classify)
    monkeypatch.setattr(agent_state, "_probe_state", fake_probe)

    await agent_state.poll_agent_states()

    assert peak == 2, f"probes ran {peak} at a time — expected both in flight"


async def test_one_raising_probe_does_not_sink_the_cycle(maker, monkeypatch):
    """`_probe_state` guards the probe CALL, but anything raised around it — the lazy
    PLUGINS import, task construction, or a probe ending CancelledError, which its
    `except Exception` does not catch and `t.result()` re-raises — escapes into the
    gather. Uncontained, that aborts the decision pass for EVERY card (and a
    CancelledError takes the poller with it, since _run_poller re-raises). The card
    whose probe broke must abstain, and it must NOT be promoted off the failure: a
    returned exception is truthy, so a tolerated one would read as "probe says
    running"."""
    async with maker() as s:
        ids = {}
        for n in ("ACE-X1", "ACE-X2"):
            card = Card(origin="jira", external_id=n, title=n, cached={})
            s.add(card)
            await s.flush()
            ids[n] = card.id
            s.add(
                TerminalSession(
                    card_id=card.id, kind="own", tmux_session=f"conductor-{n.lower()}",
                    host=None, status="live",
                )
            )
        await s.commit()

    async def fake_list_tmux():
        return [_sess("conductor-ace-x1", "conductor"), _sess("conductor-ace-x2", "conductor")]

    async def fake_classify(name, host=None, sustain_running=False):
        return ("waiting", None, None)  # both quiet + no dialog → both get probed

    async def fake_probe(card_id, sessions, was_running):
        if card_id == ids["ACE-X1"]:
            raise RuntimeError("plugin blew up")
        return ("running", "team 3/8 tasks")

    monkeypatch.setattr(term, "list_tmux", fake_list_tmux)
    monkeypatch.setattr(agent_state, "_classify_pane", fake_classify)
    monkeypatch.setattr(agent_state, "_probe_state", fake_probe)

    await agent_state.poll_agent_states()  # must not raise

    async with maker() as s:
        broke = await s.get(Card, ids["ACE-X1"])
        fine = await s.get(Card, ids["ACE-X2"])
    broke_agent = (broke.cached or {}).get("agent") or {}
    fine_agent = (fine.cached or {}).get("agent") or {}
    assert broke_agent.get("running") is not True, "a crashed probe promoted its card"
    assert fine_agent.get("running") is True, "one bad probe sank the other card's decision"
    assert fine_agent.get("bg") == "team 3/8 tasks"
