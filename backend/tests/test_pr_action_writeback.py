"""A PR action must write its outcome back onto the card.

Conductor renders the board from `cached`, so `gh pr review --approve` succeeding
changes nothing a user can see. Worse, the frontend refetches immediately afterwards,
which reads as "checked, unchanged" rather than "not looked at yet" — the report that
prompted this was exactly that confusion.

Nothing else repairs it quickly: `my_review` is written only by _maybe_brief_pr on the
dates loop (and only for cards the review-requested poll doesn't already cover), and
`pr_detail` — what both the poll and the drawer-open refresh use — does not carry
`my_review` at all, so reopening the card doesn't help either. gh mocked; DB-backed
like test_github_enrich."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from conductor import db as db_mod
from conductor import notify
from conductor.actions import pr as pr_action
from conductor.config import settings
from conductor.main import app
from conductor.models import Card
from conductor.plugins.src_github import github as github_src

REPO, NUM = "your-org/svc-security-filing", 978
EID = f"{REPO}#{NUM}"

# an automated review tool posts its recommendation as a COMMENTED review under MY
# login — that is what renders as the "🤖 approve" chip, and what a real approval must
# replace.
SUGGESTED = {
    "author": {"login": "me"}, "state": "COMMENTED", "submittedAt": "2026-08-18T06:06:08Z",
    "body": "🤖 **Suggested verdict: APPROVE** looks good",
}
APPROVED = {
    "author": {"login": "me"}, "state": "APPROVED", "submittedAt": "2026-08-21T05:30:00Z",
    "body": "",
}


@pytest.fixture
async def client(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/pr-writeback.db")
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", maker)
    monkeypatch.setattr(github_src, "session_maker", maker, raising=False)
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(github_src, "_my_login_cache", "me", raising=False)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(notify, "need_human", _noop)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c, maker
    await engine.dispose()


async def _seed(maker, **gh) -> str:
    async with maker() as s:
        card = Card(
            origin="github", external_id=EID, title="LDS documents", ball="human",
            driver="none",
            cached={"github": {"repo": REPO, "number": NUM, "state": "open",
                               "my_review": "SUGGEST_APPROVE", **gh}},
        )
        s.add(card)
        await s.commit()
        return card.id


def _stub_github(monkeypatch, *, reviews, state="open"):
    """gh is never really invoked: the action is stubbed, and the two reads the
    write-back makes are stubbed to whatever GitHub would now report."""
    calls: list[tuple] = []

    async def fake_approve(repo, number, body=None):
        calls.append(("approve", repo, number))
        return {"ok": True}

    async def fake_merge(repo, number, method="squash"):
        calls.append(("merge", repo, number))
        return {"ok": True, "detail": "merged"}

    async def fake_detail(repo, number):
        # `my_review` rides pr_detail (that is what lets the POLL maintain it, not just
        # this action), so the stub derives it exactly as the real one does — only the
        # network is faked, the verdict logic under test is real.
        return {"state": state, "ci": "passing", "review": "approved", "checks": [],
                "reviews": [], "review_requests": [],
                "my_review": github_src._review_status(
                    github_src._my_latest_review(reviews, "me")
                )}

    monkeypatch.setattr(pr_action, "approve", fake_approve)
    monkeypatch.setattr(pr_action, "merge", fake_merge)
    monkeypatch.setattr(github_src, "pr_detail", fake_detail)
    return calls


async def _gh(maker, cid: str) -> dict:
    async with maker() as s:
        card = await s.get(Card, cid)
    return (card.cached or {}).get("github") or {}


async def test_approve_replaces_the_suggested_badge_on_the_card(client, monkeypatch):
    """The reported bug: approve lands on GitHub, the chip keeps saying the automated
    tool's SUGGEST_APPROVE because nothing wrote the outcome back."""
    c, maker = client
    cid = await _seed(maker)
    _stub_github(monkeypatch, reviews=[SUGGESTED, APPROVED])

    r = await c.post(f"/api/cards/{cid}/pr/approve")

    assert r.status_code == 200 and r.json()["ok"] is True
    assert (await _gh(maker, cid))["my_review"] == "APPROVED"


async def test_merge_lands_the_card_in_done(client, monkeypatch):
    """Same gap, more visible: without the write-back `state` stays "open", so
    recompute_ball keeps a merged PR in Need Human until the dates loop runs."""
    c, maker = client
    cid = await _seed(maker)
    _stub_github(monkeypatch, reviews=[SUGGESTED, APPROVED], state="merged")

    r = await c.post(f"/api/cards/{cid}/pr/merge")

    assert r.status_code == 200
    async with maker() as s:
        card = await s.get(Card, cid)
    assert (card.cached["github"])["state"] == "merged"
    assert card.ball == "none", "a merged PR must fall out of Need Human"


async def test_no_review_of_mine_clears_the_badge(client, monkeypatch):
    """`my_review` of None is a real value — I have no review on this PR — so it must
    overwrite a stale one rather than being filtered out as empty."""
    c, maker = client
    cid = await _seed(maker)
    _stub_github(monkeypatch, reviews=[{"author": {"login": "someone-else"},
                                        "state": "APPROVED", "submittedAt": "2026-08-21T00:00:00Z"}])

    await c.post(f"/api/cards/{cid}/pr/approve")

    assert (await _gh(maker, cid))["my_review"] is None


async def test_the_action_still_succeeds_when_the_write_back_fails(client, monkeypatch):
    """The PR really was approved; reporting the request as failed because a follow-up
    read hiccuped would tell the user the opposite of what happened."""
    c, maker = client
    cid = await _seed(maker)
    _stub_github(monkeypatch, reviews=[APPROVED])

    async def boom(repo, number):
        raise RuntimeError("gh rate limited")

    monkeypatch.setattr(github_src, "pr_detail", boom)

    r = await c.post(f"/api/cards/{cid}/pr/approve")

    assert r.status_code == 200 and r.json()["ok"] is True
    assert (await _gh(maker, cid))["my_review"] == "SUGGEST_APPROVE"  # unchanged, not corrupted


async def test_unknown_action_is_still_rejected(client, monkeypatch):
    c, maker = client
    cid = await _seed(maker)
    _stub_github(monkeypatch, reviews=[APPROVED])
    assert (await c.post(f"/api/cards/{cid}/pr/nope")).status_code == 400


# --- the poll must maintain the badge too -----------------------------------------
# Codex on PR #31: a post-action read that lands before GitHub exposes the new review
# writes the OLD verdict, and on an ordinary review-requested card nothing ever
# repaired it — every writer of `my_review` went through brief_prs, which only visits
# cards the review-requested poll doesn't already cover. Carrying it on pr_detail is
# what closes that: the poll paths now maintain it, so a stale write self-heals on the
# next cycle instead of sticking forever.


async def test_pr_detail_reports_my_verdict(monkeypatch):
    """Derived from `latestReviews`, which pr_detail already fetches — no second gh
    call. This is the property every poll path depends on."""
    import json as _json

    async def fake_run_gh(args, timeout=20):
        return _json.dumps({
            "state": "OPEN", "statusCheckRollup": [], "reviewDecision": "APPROVED",
            "headRefName": "b", "title": "t", "reviewRequests": [],
            "author": {"login": "weig"},
            "latestReviews": [
                {"author": {"login": "someone-else"}, "state": "COMMENTED", "body": ""},
                {"author": {"login": "me"}, "state": "APPROVED", "body": ""},
            ],
        })

    monkeypatch.setattr(github_src, "_run_gh", fake_run_gh)
    monkeypatch.setattr(github_src, "_my_login_cache", "me", raising=False)
    detail = await github_src.pr_detail(REPO, NUM)
    assert detail["my_review"] == "APPROVED"


async def test_a_stale_badge_is_repaired_by_the_poll(client, monkeypatch):
    """The self-heal Codex asked for. A card left holding a stale SUGGEST_APPROVE must
    come back APPROVED on the next poll, WITHOUT going through brief_prs — this card is
    deliberately review-requested, the case brief_prs skips and which used to stick
    forever."""
    _c, maker = client
    async with maker() as s:
        card = Card(
            origin="github", external_id="your-org/other#1", title="plain PR",
            ball="human", driver=None,
            cached={"github": {"repo": "your-org/other", "number": 1, "state": "open",
                               "review_requested_me": True,  # brief_prs would skip this card
                               "my_review": "SUGGEST_APPROVE"}},  # the stale write
        )
        s.add(card)
        await s.commit()
        cid = card.id

    async def fake_fetch_prs():
        return [{"number": 1, "url": "u", "title": "plain PR", "state": "OPEN",
                 "repository": {"nameWithOwner": "your-org/other"},
                 "author": {"login": "weig"}}]

    async def fake_detail(repo, number):
        return {"state": "open", "ci": "passing", "review": "approved", "checks": [],
                "reviews": [], "review_requests": [], "author": "weig",
                "my_review": "APPROVED"}

    monkeypatch.setattr(github_src, "fetch_prs", fake_fetch_prs)
    monkeypatch.setattr(github_src, "pr_detail", fake_detail)

    await github_src.poll()

    async with maker() as s:
        card = await s.get(Card, cid)
    assert (card.cached["github"])["my_review"] == "APPROVED"
