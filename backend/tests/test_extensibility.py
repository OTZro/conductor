"""The core extensibility surface added for plugin-provided SOURCES: the generic
signal vocabulary in recompute_ball, PollSpec registration in /api/status, NotifySpec
channel fan-out, and the cached.badges convention — proven with synthetic plugins so
a brand-new source works end to end with ZERO core edits. sqlite-backed."""

from __future__ import annotations

import asyncio
import sys

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from conductor import db as db_mod
from conductor import main as main_mod
from conductor import notify, status, store
from conductor.config import settings
from conductor.lanes import recompute_ball
from conductor.main import app
from conductor.plugins import PLUGINS, NotifySpec, Plugin, PollSpec


@pytest.fixture
async def client(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/ext-test.db")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", maker)
    monkeypatch.setattr(settings, "auth_enabled", False)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c
    await engine.dispose()


@pytest.fixture(autouse=True)
def _reset_plugin_polls_cache(monkeypatch):
    """main_mod._plugin_polls() caches its result at module scope (see main.py) so
    system_status() doesn't recompute it per request. Reset it before every test so one
    test's monkeypatched PLUGINS can't leak a stale cached list into another's."""
    monkeypatch.setattr(main_mod, "_PLUGIN_POLLS", None)


async def _upsert(origin: str, ext: str, **kw):
    async with db_mod.session_maker() as s:
        return await store.upsert_card(s, origin=origin, external_id=ext, **kw)


# ── signal vocabulary: a plugin source drives ball with no core edit ─────────────


def test_signal_vocabulary_states():
    c = {"zz-src": {"signal": {"state": "needs_me", "detail": "3 approvals pending"}}}
    assert recompute_ball(c, origin="zz-src") == ("human", "3 approvals pending")
    c = {"zz-src": {"signal": {"state": "working", "detail": "syncing"}}}
    assert recompute_ball(c, origin="zz-src") == ("ai", "syncing")
    c = {"zz-src": {"signal": {"state": "done", "detail": "resolved"}}}
    assert recompute_ball(c, origin="zz-src") == ("none", "resolved")
    c = {"zz-src": {"signal": {"detail": "just info"}}}  # no state → terminal fallthrough
    assert recompute_ball(c, origin="zz-src") == ("none", "just info")


def test_signal_reads_only_the_cards_own_origin():
    """Another namespace's signal must not drive a foreign card — only cached[origin]."""
    c = {"other-src": {"signal": {"state": "needs_me"}}}
    assert recompute_ball(c, origin="zz-src") == ("none", None)
    assert recompute_ball(c) == ("none", None)  # origin-less callers unchanged


def test_signal_precedence_matches_builtin_ranks():
    # generic done is terminal-ranked: beats a live agent.waiting like jira Done does
    c = {"zz-src": {"signal": {"state": "done"}}, "agent": {"waiting": True}}
    assert recompute_ball(c, origin="zz-src")[0] == "none"
    # a live dashboard claude outranks generic working, like it outranks any claim
    c = {"zz-src": {"signal": {"state": "working"}}, "agent": {"running": True}}
    assert recompute_ball(c, origin="zz-src")[0] == "ai"
    assert recompute_ball(c, origin="zz-src")[1] == "claude · working"
    # generic needs_me yields to an open-PR human reason only via jira/github paths —
    # for a pure plugin card it lands human
    c = {"zz-src": {"signal": {"state": "needs_me"}}}
    assert recompute_ball(c, origin="zz-src")[0] == "human"


async def test_source_plugin_card_end_to_end(client):
    """The full recipe: a plugin upserts a card with the signal vocabulary → ball,
    lane, agent_state (status sentence) and origin all come out right via the API."""
    await _upsert(
        "zz-src", "TICKET-1", title="external thing",
        cached_patch={"zz-src": {"signal": {"state": "needs_me", "detail": "review the export"}}},
    )
    cards = (await client.get("/api/cards")).json()
    card = next(c for c in cards if c["external_id"] == "TICKET-1")
    assert card["origin"] == "zz-src" and card["ball"] == "human"
    assert card["lane"] == "need_human" and card["agent_state"] == "review the export"
    # flip to done via a fresh poll write → terminal
    await _upsert(
        "zz-src", "TICKET-1",
        cached_patch={"zz-src": {"signal": {"state": "done", "detail": "closed upstream"}}},
    )
    card = (await client.get(f"/api/cards/{card['id']}")).json()
    assert card["ball"] == "none" and card["lane"] == "done"


# ── PollSpec: plugin pollers ride the status/staleness framework ─────────────────


async def test_plugin_poll_listed_in_status_with_interval(client, monkeypatch):
    async def tick():
        return None

    fake = Plugin(id="zz-poller", label="Z", icon="🧪", polls=(PollSpec(name="sync", fn=tick, interval=42.0),))
    monkeypatch.setitem(PLUGINS, "zz-poller", fake)
    status.record_ok("zz-poller.sync")  # what a completed run records
    got = (await client.get("/api/status")).json()["pollers"]
    assert "zz-poller.sync" in got and got["zz-poller.sync"]["interval_s"] == 42.0


def test_plugin_polls_enumerated_for_lifespan(monkeypatch):
    async def tick():
        return None

    fake = Plugin(id="zz-poller2", label="Z", icon="🧪", polls=(PollSpec(name="sync", fn=tick, interval=7),))
    monkeypatch.setitem(PLUGINS, "zz-poller2", fake)
    polls = dict((n, i) for n, _f, i in main_mod._plugin_polls())
    assert polls["zz-poller2.sync"] == 7


def test_plugin_polls_validation_skips_bad_specs(monkeypatch):
    """Duplicate names would collapse status entries; interval <= 0 / non-finite would
    spin or hang the loop — both are logged and skipped, never scheduled."""

    async def tick():
        return None

    fake = Plugin(
        id="zz-bad", label="Z", icon="🧪",
        polls=(
            PollSpec(name="dup", fn=tick, interval=5),
            PollSpec(name="dup", fn=tick, interval=9),  # duplicate full name
            PollSpec(name="zero", fn=tick, interval=0),  # would spin
            PollSpec(name="neg", fn=tick, interval=-3),  # would spin
            PollSpec(name="inf", fn=tick, interval=float("inf")),  # would hang
            PollSpec(name="ok", fn=tick, interval=15),
        ),
    )
    monkeypatch.setitem(PLUGINS, "zz-bad", fake)
    polls = dict((n, i) for n, _f, i in main_mod._plugin_polls())
    mine = {k: v for k, v in polls.items() if k.startswith("zz-bad.")}
    assert mine == {"zz-bad.dup": 5.0, "zz-bad.ok": 15.0}


# ── NotifySpec: plugin notification channels ─────────────────────────────────────


async def test_notify_fans_out_to_plugin_channels(monkeypatch):
    seen: list[dict] = []

    async def channel(ctx: dict) -> None:
        seen.append(ctx)

    async def broken(ctx: dict) -> None:
        raise RuntimeError("channel down")

    monkeypatch.setattr(sys, "platform", "linux")  # keep osascript out of the test
    monkeypatch.setattr(settings, "ntfy_topic", "", raising=False)
    monkeypatch.setattr(notify, "_armed", True)
    monkeypatch.setitem(
        PLUGINS, "zz-notify",
        Plugin(id="zz-notify", label="Z", icon="🧪",
               notifiers=(NotifySpec(callback=channel), NotifySpec(callback=broken))),
    )
    await notify.need_human("title X", "sub Y", "REF-9")
    await asyncio.sleep(0.05)  # fire-and-forget tasks drain
    assert seen and seen[0]["event"] == "need_human"
    assert seen[0]["title"] == "title X" and seen[0]["ref"] == "REF-9"


async def test_notify_channels_respect_arming(monkeypatch):
    seen: list[dict] = []

    async def channel(ctx: dict) -> None:
        seen.append(ctx)

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(notify, "_armed", False)  # boot-burst suppression
    monkeypatch.setitem(
        PLUGINS, "zz-notify2",
        Plugin(id="zz-notify2", label="Z", icon="🧪", notifiers=(NotifySpec(callback=channel),)),
    )
    await notify.need_human("boot noise")
    await asyncio.sleep(0.05)
    assert seen == []  # suppressed until arm()


# ── badges: declarative decoration from stored data ──────────────────────────────


async def test_badges_merge_and_clear_via_upsert(client):
    await _upsert("zz-src", "B-1", title="badged",
                  cached_patch={"badges": {"zz-plugin": {"text": "3/8", "tone": "ai"}}})
    card = next(c for c in (await client.get("/api/cards")).json() if c["external_id"] == "B-1")
    assert card["cached"]["badges"]["zz-plugin"]["text"] == "3/8"
    # a second plugin's badge sub-merges beside it; clearing sets null (FE skips)
    await _upsert("zz-src", "B-1", cached_patch={"badges": {"other": {"text": "!"}}})
    await _upsert("zz-src", "B-1", cached_patch={"badges": {"zz-plugin": None}})
    card = (await client.get(f"/api/cards/{card['id']}")).json()
    assert card["cached"]["badges"]["other"]["text"] == "!"
    assert card["cached"]["badges"]["zz-plugin"] is None


# ── promote-only enrichment signals (cached.signals) ─────────────────────────────


def test_enrichment_needs_me_pulls_a_foreign_card_to_human():
    """An enricher (e.g. a Sentry plugin) flags ANOTHER source's card: needs_me is
    honored — a card nobody owned lands in Need Human with the contributor's detail."""
    c = {"jira": {"status": "In Review"}, "signals": {"sentry": {"state": "needs_me", "detail": "error spike"}}}
    assert recompute_ball(c, origin="jira") == ("human", "error spike")


def test_enrichment_may_only_promote_never_done_or_working():
    """The StateProbe philosophy: a foreign contributor must not close your card or
    claim AI holds it — done/working from cached.signals are ignored."""
    c = {"jira": {"status": "In Review"}, "signals": {"bot": {"state": "done"}}}
    assert recompute_ball(c, origin="jira")[0] == "none"  # ignored → falls through
    c = {"jira": {"status": "In Review"}, "signals": {"bot": {"state": "working"}}}
    assert recompute_ball(c, origin="jira")[0] == "none"


def test_enrichment_slots_below_live_and_owning_signals():
    """A live agent run / an assignee outrank an enrichment nudge — the slot is
    fixed at the bottom of the human-space chain, so contributors can't reorder it."""
    c = {"jira": {"status": "In Review"}, "agent": {"running": True},
         "signals": {"sentry": {"state": "needs_me"}}}
    assert recompute_ball(c, origin="jira")[0] == "ai"  # the live agent keeps it
    c = {"jira": {"status": "In Review", "assignee_me": True},
         "signals": {"sentry": {"state": "needs_me", "detail": "spike"}}}
    assert recompute_ball(c, origin="jira") == ("human", "In Review")  # assignee wins detail


def test_enrichment_deterministic_and_clearable():
    c = {"jira": {"status": "In Review"},
         "signals": {"zzz": {"state": "needs_me", "detail": "late"},
                     "aaa": {"state": "needs_me", "detail": "first"}}}
    assert recompute_ball(c, origin="jira")[1] == "first"  # sorted by contributor name
    c["signals"]["aaa"] = None  # cleared via upsert sub-merge → skipped
    assert recompute_ball(c, origin="jira")[1] == "late"


# ── plugin card-body provider ────────────────────────────────────────────────────


async def test_plugin_card_body_serves_unknown_origin(client, monkeypatch):
    async def body(ctx: dict):
        if ctx["origin"] != "zz-src":
            return None  # provider owns "does this apply"
        return {"kind": "zz-src", "content": f"rich body for {ctx['external_id']}"}

    monkeypatch.setitem(PLUGINS, "zz-body", Plugin(id="zz-body", label="Z", icon="🧪", card_body=body))
    await _upsert("zz-src", "BODY-1", title="t", summary="the summary")
    card = next(c for c in (await client.get("/api/cards")).json() if c["external_id"] == "BODY-1")
    got = (await client.get(f"/api/cards/{card['id']}/body")).json()
    assert got == {"kind": "zz-src", "content": "rich body for BODY-1"}


async def test_plugin_card_body_falls_back_on_pass_or_error(client, monkeypatch):
    async def broken(ctx: dict):
        raise RuntimeError("provider bug")

    monkeypatch.setitem(PLUGINS, "zz-body2", Plugin(id="zz-body2", label="Z", icon="🧪", card_body=broken))
    await _upsert("zz-src", "BODY-2", title="t", summary="fallback summary")
    card = next(c for c in (await client.get("/api/cards")).json() if c["external_id"] == "BODY-2")
    got = (await client.get(f"/api/cards/{card['id']}/body")).json()
    assert got == {"kind": "zz-src", "content": "fallback summary"}  # error → skipped, summary stands
