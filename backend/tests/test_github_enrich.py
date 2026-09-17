"""_enrich_one_jira: a ticket's PR links must cover ALL its matched PRs, not a top-N
slice, and cached.prs must track that set (not accumulate stale entries).

Regression for PROJ-9406: the ticket had 9 matched PRs across 5 repos; a `prs[:5]` cap
dropped whichever fell past position 5 in gh's search ranking, so the (merged) gf-id PR
link kept vanishing while its stale entry lingered in cached.prs. DB-backed, gh mocked."""

from __future__ import annotations

import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from conductor import db as db_mod
from conductor import notify
from conductor.models import Card, CardLink
from conductor.plugins.src_github import github as github_src


def _pr(repo: str, num: int, state: str = "open"):
    return {
        "number": num,
        "url": f"https://github.com/{repo}/pull/{num}",
        "repository": {"nameWithOwner": repo},
        "_detail": {"state": state, "ci": "passing", "review": "", "checks": [],
                    "reviews": [], "review_requests": []},
    }


# PROJ-9406's real shape: 9 PRs, the two gf-id ones last (merged #462, closed #457).
NINE_PRS = [
    _pr("your-org/svc-doc-engine", 114, "open"),
    _pr("your-org/fms", 19924, "open"),
    _pr("your-org/gf-external-api", 847, "closed"),
    _pr("your-org/svc-doc-engine", 119, "closed"),
    _pr("your-org/fms", 19921, "closed"),
    _pr("your-org/svc-doc-engine-fe", 148, "closed"),
    _pr("your-org/fms", 19911, "closed"),
    _pr("your-org/gf-id", 462, "merged"),
    _pr("your-org/gf-id", 457, "closed"),
]


@pytest.fixture
async def maker(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/gh-enrich.db")
    m = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", m)
    monkeypatch.setattr(github_src, "session_maker", m)  # module-bound name in github.py

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(notify, "need_human", _noop)  # no macOS notification in tests
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield m
    await engine.dispose()


async def test_enrich_links_all_matched_prs_and_replaces_stale_prs(maker, monkeypatch):
    async def fake_search(key):
        return list(NINE_PRS) if key == "PROJ-9406" else []

    async def fake_detail(repo, number):  # only the hand-linked manual#9 reaches this
        return {"state": "open", "ci": "passing", "review": "", "checks": [],
                "reviews": [], "review_requests": []}

    monkeypatch.setattr(github_src, "search_prs_for_key", fake_search)
    monkeypatch.setattr(github_src, "pr_detail", fake_detail)

    async with maker() as s:
        card = Card(
            origin="jira",
            external_id="PROJ-9406",
            title="all-office template",
            ball="human",
            cached={
                "jira": {"status": "In Progress", "assignee_me": True},
                # a stale PR that no longer matches + a stale auto link for it: both must
                # be swept, proving prs is replaced (not merged) and links self-heal.
                "prs": {"your-org/old#1": {"state": "closed", "ci": "failing"}},
            },
        )
        s.add(card)
        await s.flush()
        s.add(CardLink(card_id=card.id, kind="pr", ref="your-org/old#1",
                       url="x", title="old", auto=True))
        # a human-added link must survive the self-heal (only auto pr links are swept)
        s.add(CardLink(card_id=card.id, kind="pr", ref="your-org/manual#9",
                       url="y", title="manual", auto=False))
        await s.commit()
        cid = card.id
        card_obj = await s.get(Card, cid)

    now = datetime.datetime.now(datetime.timezone.utc)
    attached = await github_src._enrich_one_jira(card_obj, now)
    assert attached is True

    async with maker() as s:
        links = (
            await s.execute(select(CardLink).where(CardLink.card_id == cid))
        ).scalars().all()
        auto_pr_refs = {link.ref for link in links if link.kind == "pr" and link.auto}
        # all nine matched PRs are linked — including BOTH gf-id PRs that used to fall
        # past the [:5] cap
        assert auto_pr_refs == {
            "your-org/svc-doc-engine#114", "your-org/fms#19924",
            "your-org/gf-external-api#847", "your-org/svc-doc-engine#119",
            "your-org/fms#19921", "your-org/svc-doc-engine-fe#148",
            "your-org/fms#19911", "your-org/gf-id#462", "your-org/gf-id#457",
        }
        assert "your-org/old#1" not in auto_pr_refs  # stale auto link swept
        # the manual link is untouched
        assert any(link.ref == "your-org/manual#9" and not link.auto for link in links)

        card = await s.get(Card, cid)
        prs = card.cached["prs"]
        # cached.prs is REPLACED with the current set — the 9 searched PRs plus the
        # hand-linked manual#9 (now enriched too); stale old#1 gone, no accumulation
        assert set(prs) == auto_pr_refs | {"your-org/manual#9"}
        assert prs["your-org/manual#9"]["state"] == "open"  # hand link got a status
        assert "your-org/old#1" not in prs
        # states preserved per PR (merged gf-id shows, closed gf-id present for the
        # board's closed-PR filter)
        assert prs["your-org/gf-id#462"]["state"] == "merged"
        assert prs["your-org/gf-id#457"]["state"] == "closed"


async def test_enrich_covers_hand_linked_prs_the_search_misses(maker, monkeypatch):
    # PROJ-10778 pinned PROJ-6359's PR (branch fix/PROJ-6359-…) by hand; the PROJ-10778
    # key-search never matches it, so without enriching manual links it had no status
    # preview. Enrich must fetch its detail into cached.prs anyway.
    async def no_search(key):
        return []  # the key-search finds nothing

    async def fake_detail(repo, number):
        assert (repo, number) == ("your-org/fms", 19978)
        return {
            "state": "open", "ci": "passing", "review": "changes_requested",
            "checks": [], "reviews": [{"user": "coderabbitai", "state": "approved"}],
            "review_requests": [],
        }

    monkeypatch.setattr(github_src, "search_prs_for_key", no_search)
    monkeypatch.setattr(github_src, "pr_detail", fake_detail)

    async with maker() as s:
        card = Card(
            origin="jira", external_id="PROJ-10778", title="hand-linked pr", ball="human",
            cached={"jira": {"status": "Building", "assignee_me": True}},
        )
        s.add(card)
        await s.flush()
        s.add(CardLink(
            card_id=card.id, kind="pr", ref="your-org/fms#19978",
            url="https://github.com/your-org/fms/pull/19978", title="x", auto=False,
        ))
        await s.commit()
        cid = card.id
        card_obj = await s.get(Card, cid)

    now = datetime.datetime.now(datetime.timezone.utc)
    stored = await github_src._enrich_one_jira(card_obj, now)
    assert stored is True  # status stored even with zero searched (auto) PRs

    async with maker() as s:
        card = await s.get(Card, cid)
        meta = card.cached["prs"].get("your-org/fms#19978")
        assert meta is not None  # the missing preview's data is now present
        assert meta["review"] == "changes_requested"
        assert meta["reviews"] == [{"user": "coderabbitai", "state": "approved"}]
        # the hand link is untouched (self-heal only sweeps auto pr links)
        links = (
            await s.execute(select(CardLink).where(CardLink.card_id == cid))
        ).scalars().all()
        pr_links = [link for link in links if link.kind == "pr"]
        assert len(pr_links) == 1 and pr_links[0].ref == "your-org/fms#19978"
        assert pr_links[0].auto is False


async def test_manual_pr_status_for_non_jira_card(maker, monkeypatch):
    # a slack (or manual) card with a hand-linked PR must get a status preview too, via
    # refresh_card_prs — enrich_one_jira never runs for it.
    async def fake_detail(repo, number):
        assert (repo, number) == ("your-org/fms", 19845)
        return {
            "state": "open", "ci": "failing", "review": "changes_requested",
            "checks": [{"name": "ci / test", "state": "failing"}],
            "reviews": [{"user": "zingcs86", "state": "commented"}], "review_requests": [],
        }

    monkeypatch.setattr(github_src, "pr_detail", fake_detail)

    async with maker() as s:
        card = Card(
            origin="slack", external_id="mention:C1:123.456", title="Eva handed it over",
            ball="human", cached={"slack": {"from": "Eva Cheng"}},
        )
        s.add(card)
        await s.flush()
        s.add(CardLink(
            card_id=card.id, kind="pr", ref="your-org/fms#19845",
            url="https://github.com/your-org/fms/pull/19845", title="x", auto=False,
        ))
        await s.commit()
        cid = card.id

    assert await github_src.refresh_card_prs(cid) is True

    async with maker() as s:
        meta = (await s.get(Card, cid)).cached["prs"].get("your-org/fms#19845")
        assert meta is not None and meta["ci"] == "failing"
        assert meta["reviews"] == [{"user": "zingcs86", "state": "commented"}]


async def test_manual_pr_status_keeps_stale_value_on_transient_fetch_failure(maker, monkeypatch):
    """A gh hiccup on one enrichment cycle must not blank out a PR chip's last-known
    status. `pr_detail` swallows its own errors and returns `{}` on failure (it never
    raises), so `_add_manual_pr_status` must treat that as a signal to fall back to the
    card's prior cached.prs entry for the ref rather than overwrite it with an
    all-blank status."""
    async def failing_detail(repo, number):
        return {}  # pr_detail's own failure signal — network/timeout/gh error

    monkeypatch.setattr(github_src, "pr_detail", failing_detail)

    prior_status = {
        "state": "open", "ci": "passing", "review": "approved",
        "checks": [], "reviews": [{"user": "alex", "state": "approved"}],
        "review_requests": [], "author": "alex",
    }
    async with maker() as s:
        card = Card(
            origin="slack", external_id="mention:C1:999.1", title="flaky gh",
            ball="human",
            cached={"slack": {"from": "Eva Cheng"},
                    "prs": {"your-org/fms#19845": dict(prior_status)}},
        )
        s.add(card)
        await s.flush()
        s.add(CardLink(
            card_id=card.id, kind="pr", ref="your-org/fms#19845",
            url="https://github.com/your-org/fms/pull/19845", title="x", auto=False,
        ))
        await s.commit()
        cid = card.id

    # the stale value still counts as "attached" — the chip must keep showing it
    assert await github_src.refresh_card_prs(cid) is True

    async with maker() as s:
        meta = (await s.get(Card, cid)).cached["prs"].get("your-org/fms#19845")
    assert meta == prior_status  # unchanged, not blanked out by the failed fetch


async def test_every_pr_link_gets_status_regardless_of_auto(maker, monkeypatch):
    """A PR chip always carries its status hover — auto-discovered (auto=True) and
    plugin-attached links enrich exactly like hand-pinned ones. (auto keeps only its
    jira self-heal role.)"""
    seen: list[tuple[str, int]] = []

    async def fake_detail(repo, number):
        seen.append((repo, number))
        return {
            "state": "open", "ci": "passing", "review": "approved",
            "checks": [], "reviews": [], "review_requests": [],
        }

    monkeypatch.setattr(github_src, "pr_detail", fake_detail)

    async with maker() as s:
        card = Card(origin="zz-src", external_id="LIN-1", title="plugin source card",
                    ball="human", cached={})
        s.add(card)
        await s.flush()
        s.add(CardLink(card_id=card.id, kind="pr", ref="your-org/fms#111",
                       url="u1", title="scraped", auto=True))   # auto-discovered
        s.add(CardLink(card_id=card.id, kind="pr", ref="your-org/fms#222",
                       url="u2", title="attached", auto=False))  # deliberately attached
        await s.commit()
        cid = card.id

    assert await github_src.refresh_card_prs(cid) is True
    async with maker() as s:
        prs = (await s.get(Card, cid)).cached["prs"]
    assert set(prs) == {"your-org/fms#111", "your-org/fms#222"}  # BOTH enriched
    assert set(seen) == {("your-org/fms", 111), ("your-org/fms", 222)}
