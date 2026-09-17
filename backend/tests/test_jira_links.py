"""Linked-jira hover enrichment: every jira link chip carries ticket info (title /
status / assignee) in cached.jiras — however the link was attached (auto-discovered or
hand-pinned), on any origin. A jira card's OWN ticket is skipped (its data already
rides cached.jira). sqlite-backed, acli mocked."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from conductor import db as db_mod
from conductor import notify
from conductor.models import Card, CardLink
from conductor.plugins.src_jira import jira as jira_src


@pytest.fixture
async def maker(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/jira-links.db")
    m = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", m)
    monkeypatch.setattr(jira_src, "session_maker", m)  # module-bound name in jira.py

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(notify, "need_human", _noop)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield m
    await engine.dispose()


@pytest.fixture
def fake_brief(monkeypatch):
    seen: list[str] = []

    async def brief(key: str) -> dict:
        seen.append(key)
        return {"title": f"title of {key}", "status": "In Progress", "assignee": "Alex"}

    monkeypatch.setattr(jira_src, "fetch_brief", brief)
    return seen


async def test_pr_cards_jira_link_gets_hover_data_regardless_of_auto(maker, fake_brief):
    """A github PR card's auto-discovered ACE link — the everyday case — enriches."""
    async with maker() as s:
        card = Card(origin="github", external_id="your-org/fms#1", title="pr job",
                    ball="human", cached={})
        s.add(card)
        await s.flush()
        s.add(CardLink(card_id=card.id, kind="jira", ref="PROJ-123",
                       url="https://x/browse/PROJ-123", title="PROJ-123", auto=True))
        await s.commit()
        cid = card.id

    assert await jira_src.enrich_linked_jiras() == 1
    async with maker() as s:
        card = await s.get(Card, cid)
    assert card.cached["jiras"]["PROJ-123"]["title"] == "title of PROJ-123"
    assert card.cached["jiras"]["PROJ-123"]["status"] == "In Progress"
    assert fake_brief == ["PROJ-123"]
    assert card.cached.get("jira_link_ts")  # throttle bookkeeping written


async def test_jira_cards_self_link_skipped_but_cross_links_enrich(maker, fake_brief):
    """A jira card links its OWN ticket (board chip — data already on the card) plus a
    related ticket discovered from its description: only the related one fetches."""
    async with maker() as s:
        card = Card(origin="jira", external_id="PROJ-100", title="self", ball="human",
                    cached={"jira": {"status": "Building"}})
        s.add(card)
        await s.flush()
        s.add(CardLink(card_id=card.id, kind="jira", ref="PROJ-100", url="u", auto=True))
        s.add(CardLink(card_id=card.id, kind="jira", ref="PROJ-200", url="u2", auto=False))
        await s.commit()
        cid = card.id

    await jira_src.enrich_linked_jiras()
    async with maker() as s:
        card = await s.get(Card, cid)
    assert set(card.cached["jiras"]) == {"PROJ-200"}  # self skipped, cross-link enriched
    assert fake_brief == ["PROJ-200"]


async def test_enrich_throttled_and_ts_does_not_churn_updated_at(maker, fake_brief):
    async with maker() as s:
        card = Card(origin="slack", external_id="mention:C1:1.2", title="s", ball="human",
                    cached={})
        s.add(card)
        await s.flush()
        s.add(CardLink(card_id=card.id, kind="jira", ref="PROJ-9", url="u", auto=True))
        await s.commit()
        cid = card.id

    await jira_src.enrich_linked_jiras()
    async with maker() as s:
        first = await s.get(Card, cid)
        updated_after_first = first.updated_at
    # second cycle: fresh ts → throttled, no re-fetch
    assert await jira_src.enrich_linked_jiras() == 0
    assert fake_brief == ["PROJ-9"]  # fetched exactly once
    # the ts-only write must not have churned updated_at ordering on the next real poll
    async with maker() as s:
        card = await s.get(Card, cid)
    assert card.updated_at == updated_after_first


async def test_linked_jira_mirrors_its_pr_card_prs(maker, fake_brief):
    """The linked ticket's jira-origin card carries cached.prs (from enrich_jira_prs) —
    it must be copied onto cached.jiras[ticket].prs so a card that only LINKS to the
    ticket (officraft/slack/manual origin) shows its PRs too."""
    prs = {"your-org/fms#20379": {"state": "open", "ci": "passing", "review": "approved"}}
    async with maker() as s:
        linker = Card(origin="officraft", external_id="task:t-1", title="task", ball="human",
                       cached={})
        s.add(linker)
        jira_card = Card(origin="jira", external_id="PROJ-10762", title="the ticket",
                          ball="human", cached={"prs": prs})
        s.add(jira_card)
        await s.flush()
        s.add(CardLink(card_id=linker.id, kind="jira", ref="PROJ-10762",
                       url="https://x/browse/PROJ-10762", title="PROJ-10762", auto=True))
        await s.commit()
        cid = linker.id

    assert await jira_src.enrich_linked_jiras() == 1
    async with maker() as s:
        card = await s.get(Card, cid)
    assert card.cached["jiras"]["PROJ-10762"]["prs"] == prs


async def test_linked_jira_omits_prs_key_when_jira_card_has_none(maker, fake_brief):
    """No jira-origin card for the ticket (or one with no cached.prs) → the `prs` key
    is simply absent, not an empty dict — the FE treats missing the same as none."""
    async with maker() as s:
        linker = Card(origin="slack", external_id="mention:C1:2.1", title="s", ball="human",
                      cached={})
        s.add(linker)
        await s.flush()
        s.add(CardLink(card_id=linker.id, kind="jira", ref="PROJ-999", url="u", auto=True))
        await s.commit()
        cid = linker.id

    await jira_src.enrich_linked_jiras()
    async with maker() as s:
        card = await s.get(Card, cid)
    assert "prs" not in card.cached["jiras"]["PROJ-999"]


async def test_linked_jira_prs_drop_when_jira_card_prs_emptied_out(maker, fake_brief):
    """cached_replace on `jiras` must fully rebuild each ref's entry every cycle — if
    the jira card's cached.prs empties out (all PRs merged/closed and swept), a stale
    `prs` copy from a previous poll cycle must not linger on the linker card."""
    async with maker() as s:
        linker = Card(origin="manual", external_id="m2", title="m", ball="human", cached={})
        s.add(linker)
        jira_card = Card(origin="jira", external_id="PROJ-321", title="t", ball="human",
                          cached={"prs": {"your-org/fms#1": {"state": "open"}}})
        s.add(jira_card)
        await s.flush()
        s.add(CardLink(card_id=linker.id, kind="jira", ref="PROJ-321", url="u", auto=True))
        await s.commit()
        cid, jira_id = linker.id, jira_card.id

    await jira_src.enrich_linked_jiras()
    async with maker() as s:
        card = await s.get(Card, cid)
    assert card.cached["jiras"]["PROJ-321"]["prs"] == {"your-org/fms#1": {"state": "open"}}

    # jira card's PRs empty out (e.g. merged + swept) and the linker becomes stale again
    async with maker() as s:
        jira_card = await s.get(Card, jira_id)
        jira_card.cached = {"prs": {}}
        card = await s.get(Card, cid)
        card.cached = {**card.cached, "jira_link_ts": None}
        await s.commit()

    await jira_src.enrich_linked_jiras()
    async with maker() as s:
        card = await s.get(Card, cid)
    assert "prs" not in card.cached["jiras"]["PROJ-321"]


async def test_unreadable_ticket_just_stays_plain(maker, monkeypatch):
    async def boom(key: str) -> dict:
        raise RuntimeError("acli down")

    monkeypatch.setattr(jira_src, "fetch_brief", boom)
    async with maker() as s:
        card = Card(origin="manual", external_id="m1", title="m", ball="human", cached={})
        s.add(card)
        await s.flush()
        s.add(CardLink(card_id=card.id, kind="jira", ref="PROJ-77", url="u", auto=True))
        await s.commit()
        cid = card.id

    assert await jira_src.enrich_linked_jiras() == 0  # nothing stored, nothing raised
    async with maker() as s:
        card = await s.get(Card, cid)
    assert card.cached.get("jiras") == {}  # chip renders plain until jira is reachable