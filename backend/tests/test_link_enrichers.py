"""LinkEnricherSpec: the generic link-kind enrichment loop (plugin declares kind +
fetch; core finds cards, throttles, snapshots into cached.linkmeta) and the jira
transition endpoint (status picker vocabulary + acli-validated move + refreshes).
sqlite-backed, acli/fetch mocked."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from conductor import db as db_mod
from conductor import enrich, notify
from conductor.config import settings
from conductor.main import app
from conductor.models import Card, CardLink
from conductor.plugins import PLUGINS, LinkEnricherSpec, Plugin
from conductor.plugins.src_jira import jira as jira_src


@pytest.fixture
async def client(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/enrich-test.db")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", maker)
    monkeypatch.setattr(enrich, "session_maker", maker)  # module-bound name
    monkeypatch.setattr(jira_src, "session_maker", maker)
    monkeypatch.setattr(settings, "auth_enabled", False)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(notify, "need_human", _noop)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c
    await engine.dispose()


async def _card_with_link(kind: str, ref: str) -> str:
    async with db_mod.session_maker() as s:
        card = Card(origin="manual", external_id=f"m-{ref}", title="t", ball="human", cached={})
        s.add(card)
        await s.flush()
        s.add(CardLink(card_id=card.id, kind=kind, ref=ref, url="u", auto=True))
        await s.commit()
        return card.id


async def test_enricher_fills_linkmeta_and_throttles(client, monkeypatch):
    seen: list[str] = []

    async def fetch(ref: str) -> dict:
        seen.append(ref)
        return {"title": f"zd {ref}", "status": "open", "lines": ["prio: high"]}

    fake = Plugin(id="zz-zd", label="Z", icon="🧪",
                  link_enrichers=(LinkEnricherSpec(kind="zendesk", fetch=fetch),))
    monkeypatch.setitem(PLUGINS, "zz-zd", fake)
    cid = await _card_with_link("zendesk", "ZD-1")

    assert await enrich.enrich_plugin_links() == 1
    async with db_mod.session_maker() as s:
        card = await s.get(Card, cid)
    assert card.cached["linkmeta"]["zendesk"]["ZD-1"]["title"] == "zd ZD-1"
    assert card.cached.get("linkmeta_ts")
    # fresh ts → throttled, no re-fetch
    assert await enrich.enrich_plugin_links() == 0
    assert seen == ["ZD-1"]


async def test_enricher_error_keeps_chip_plain(client, monkeypatch):
    async def boom(ref: str) -> dict:
        raise RuntimeError("upstream down")

    fake = Plugin(id="zz-zd2", label="Z", icon="🧪",
                  link_enrichers=(LinkEnricherSpec(kind="zzkind", fetch=boom),))
    monkeypatch.setitem(PLUGINS, "zz-zd2", fake)
    cid = await _card_with_link("zzkind", "X-1")
    assert await enrich.enrich_plugin_links() == 0  # nothing stored, nothing raised
    async with db_mod.session_maker() as s:
        card = await s.get(Card, cid)
    assert card.cached.get("linkmeta", {}) == {}


async def test_no_enrichers_is_a_cheap_noop(client):
    assert await enrich.enrich_plugin_links() == 0


# ── jira transition endpoint ─────────────────────────────────────────────────────


async def test_transition_runs_acli_and_refreshes(client, monkeypatch):
    ran: list[list[str]] = []

    async def fake_acli(args, timeout=30):
        ran.append(list(args))
        return "{}"

    refreshed: list[str] = []

    async def fake_fetch_keys(keys):
        refreshed.extend(keys)
        return len(keys)

    monkeypatch.setattr(jira_src, "_run_acli", fake_acli)
    monkeypatch.setattr(jira_src, "fetch_keys", fake_fetch_keys)
    r = await client.post("/api/jira/PROJ-123/transition", json={"status": "Hardening"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert ran[0][:4] == ["jira", "workitem", "transition", "--key"]
    assert "Hardening" in ran[0] and "--yes" in ran[0]
    assert refreshed == ["PROJ-123"]  # the ticket's board card refreshes


async def test_transition_surfaces_jiras_refusal(client, monkeypatch):
    async def refuse(args, timeout=30):
        raise RuntimeError("transition not allowed from current status")

    monkeypatch.setattr(jira_src, "_run_acli", refuse)
    r = await client.post("/api/jira/PROJ-123/transition", json={"status": "Done"})
    assert r.status_code == 400 and "not allowed" in r.json()["detail"]


async def test_transition_validates_inputs(client):
    assert (await client.post("/api/jira/not a key/transition", json={"status": "X"})).status_code in (400, 404)
    assert (await client.post("/api/jira/PROJ-1/transition", json={})).status_code == 400


async def test_statuses_come_from_the_board(client):
    async with db_mod.session_maker() as s:
        for i, st in enumerate(["Building", "Hardening", "Building"]):
            s.add(Card(origin="jira", external_id=f"ACE-{i}", title="t", ball="human",
                       cached={"jira": {"status": st}}))
        await s.commit()
    got = (await client.get("/api/jira/statuses")).json()["statuses"]
    assert got == ["Building", "Hardening"]  # distinct + sorted