"""M5 shape-freeze + identity proofs for the source plugins.

Three families:

1. **cached.* contract tests** — for each source (jira / github / slack) the
   network layer (acli subprocess, gh subprocess, Slack Web API `_call`) is
   patched and the poll is run for real; the EXACT key set each source writes
   into its cached namespaces (jira, github, slack, prs, jiras) is asserted.
   These shapes are FROZEN — lanes.recompute_ball and the FE read them.

2. **/api/status poller-name identity** — the legacy poller names and their
   intervals survive the move of the source loops onto kernel PollSpecs,
   byte-identical (hardcoded expected set), and the source cadences are now
   genuinely served by the kernel.

3. **kernel-native source plugin e2e** — a source registered straight into the
   kernel's poll registry (no Plugin dataclass, no discovery) polls, writes its
   own cached namespace with the signal vocabulary, and its card lands in the
   right lane: a plugin can BE a source. (Cribbed from test_extensibility's
   source-plugin e2e; that file is untouched.)
"""

from __future__ import annotations

import json
import time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from conductor import db as db_mod
from conductor import main as main_mod
from conductor import notify, status, store
from conductor.config import settings
from conductor.main import app
from conductor.models import Card
from conductor.plugins import PLUGINS, PollSpec, runtime
from conductor.plugins.src_github import github as github_src
from conductor.plugins.src_jira import jira as jira_src
from conductor.plugins.src_slack import slack as slack_src


@pytest.fixture
async def maker(tmp_path, monkeypatch):
    """sqlite DB wired into db, store and EVERY source module (each binds
    session_maker at import — the established pattern, see test_github_enrich)."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/src-contracts.db")
    m = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "session_maker", m)
    monkeypatch.setattr(store, "session_maker", m)
    for mod in (jira_src, github_src, slack_src):
        monkeypatch.setattr(mod, "session_maker", m)

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(notify, "need_human", _noop)  # no desktop pings from tests
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield m
    await engine.dispose()


@pytest.fixture
async def client(maker, monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_plugin_polls_cache(monkeypatch):
    monkeypatch.setattr(main_mod, "_PLUGIN_POLLS", None)


async def _card(maker, origin: str, ext: str) -> Card:
    async with maker() as s:
        return (
            await s.execute(select(Card).where(Card.origin == origin, Card.external_id == ext))
        ).scalar_one()


# ── 1a. jira: cached.jira written by poll ────────────────────────────────────────

JIRA_KEYS = {
    "status", "status_category", "labels", "priority", "assignee_me", "pinned",
    "issuetype", "is_subtask", "parent_key", "parent_summary",
}

_JIRA_ITEM = {
    "key": "PROJ-9",
    "fields": {
        "summary": "Fix the widget",
        "status": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}},
        "labels": ["urgent"],
        "priority": {"name": "P2"},
        "issuetype": {"name": "Task", "subtask": False},
        "updated": "2026-09-01T00:00:00.000+0000",
    },
}


async def test_jira_poll_cached_shape(maker, monkeypatch):
    async def fake_acli(args, timeout=30):
        assert args[:3] == ["jira", "workitem", "search"]
        return json.dumps({"workItems": [_JIRA_ITEM]})

    monkeypatch.setattr(jira_src, "_run_acli", fake_acli)
    assert await jira_src.poll() == 1
    card = await _card(maker, "jira", "PROJ-9")
    j = card.cached["jira"]
    assert set(j) == JIRA_KEYS  # FROZEN — recompute_ball + FE read these
    assert j["status"] == "In Progress" and j["status_category"] == "indeterminate"
    assert j["labels"] == ["urgent"] and j["priority"] == "P2"
    assert j["assignee_me"] is True and j["pinned"] is False
    assert j["issuetype"] == "Task" and j["is_subtask"] is False
    assert j["parent_key"] is None and j["parent_summary"] is None


# ── 1b. jira: cached.jiras written by the linked-jira enrichment ────────────────


async def test_jira_link_enrich_cached_shape(maker, monkeypatch):
    async with maker() as s:
        await store.upsert_card(
            s, origin="slack", external_id="mention:C1:1.0", title="ping",
            extra_links=[{"kind": "jira", "ref": "PROJ-77", "url": "https://j/PROJ-77"}],
        )

    async def fake_acli(args, timeout=30):
        assert args[:4] == ["jira", "workitem", "view", "PROJ-77"]
        return json.dumps({"fields": {
            "summary": "Linked ticket", "status": {"name": "Done"},
            "assignee": {"displayName": "Amy"},
        }})

    monkeypatch.setattr(jira_src, "_run_acli", fake_acli)
    assert await jira_src.enrich_linked_jiras() == 1
    card = await _card(maker, "slack", "mention:C1:1.0")
    assert set(card.cached["jiras"]) == {"PROJ-77"}
    assert set(card.cached["jiras"]["PROJ-77"]) == {"title", "status", "assignee"}  # FROZEN
    assert card.cached["jiras"]["PROJ-77"] == {
        "title": "Linked ticket", "status": "Done", "assignee": "Amy",
    }
    assert "jira_link_ts" in card.cached  # the per-card throttle stamp


# ── 1c. github: cached.github written by poll ───────────────────────────────────

GITHUB_KEYS = {
    "number", "repo", "url", "state", "is_draft", "author", "review_requested_me",
    "reviewed_by_me", "ci", "review", "my_review", "checks", "reviews",
    "review_requests",
}

_GH_PR = {
    "number": 7,
    "title": "PROJ-9 fix the widget",
    "url": "https://github.com/acme/widgets/pull/7",
    "repository": {"nameWithOwner": "acme/widgets"},
    "author": {"login": "amy"},
    "isDraft": False,
    "state": "OPEN",
}

_GH_DETAIL = {
    "state": "OPEN",
    "statusCheckRollup": [],
    "reviewDecision": "",
    "headRefName": "fix-widget",
    "title": "PROJ-9 fix the widget",
    "latestReviews": [],
    "reviewRequests": [],
    "author": {"login": "amy"},
}


def _fake_gh(monkeypatch):
    async def fake_gh(args, timeout=20):
        if args[0] == "search":
            return json.dumps([_GH_PR])
        if args[0] == "pr":
            return json.dumps(_GH_DETAIL)
        if args[0] == "api":  # gh api user --jq .login
            return "me\n"
        raise AssertionError(f"unexpected gh call: {args}")

    monkeypatch.setattr(github_src, "_run_gh", fake_gh)
    monkeypatch.setattr(github_src, "_my_login_cache", None)


async def test_github_poll_cached_shape(maker, monkeypatch):
    _fake_gh(monkeypatch)
    assert await github_src.poll() == 1
    card = await _card(maker, "github", "acme/widgets#7")
    gh = card.cached["github"]
    assert set(gh) == GITHUB_KEYS  # FROZEN — recompute_ball + FE read these
    assert gh["number"] == 7 and gh["repo"] == "acme/widgets"
    assert gh["state"] == "open" and gh["is_draft"] is False
    assert gh["author"] == "amy"
    assert gh["review_requested_me"] is True and gh["reviewed_by_me"] is False
    assert gh["checks"] == [] and gh["reviews"] == [] and gh["review_requests"] == []


# ── 1d. github: cached.prs written by the jira-PR enrichment ────────────────────

PR_STATUS_KEYS = {"state", "ci", "review", "checks", "reviews", "review_requests", "author"}


async def test_github_jira_pr_enrich_cached_shape(maker, monkeypatch):
    _fake_gh(monkeypatch)
    async with maker() as s:
        await store.upsert_card(
            s, origin="jira", external_id="PROJ-9", title="Fix the widget",
            cached_patch={"jira": {"status": "In Progress", "assignee_me": True}},
        )
    assert await github_src.enrich_jira_prs() == 1
    card = await _card(maker, "jira", "PROJ-9")
    assert set(card.cached["prs"]) == {"acme/widgets#7"}
    entry = card.cached["prs"]["acme/widgets#7"]
    assert set(entry) == PR_STATUS_KEYS  # FROZEN — the PR chip/hover contract
    assert entry["state"] == "open" and entry["author"] == "amy"
    assert "pr_search_ts" in card.cached  # the per-card throttle stamp
    # manual/slack/plugin cards share the same writer (_pr_status_entry via
    # enrich_manual_prs), so this key set covers cached.prs for every origin.


# ── 1e. slack: cached.slack written by poll ─────────────────────────────────────

SLACK_KEYS = {"unread", "kind", "channel", "ts", "text", "from"}


async def test_slack_poll_cached_shape(maker, monkeypatch):
    now_ts = f"{time.time():.6f}"
    match = {
        "ts": now_ts,
        "channel": {"id": "C1", "name": "general"},
        "text": "<@U1> please review the export",
        "permalink": "https://acme.slack.com/archives/C1/p1",
        "user": "U2",
    }

    async def fake_call(method, token, params):
        if method == "search.messages":
            return {"messages": {"matches": [match]}}
        if method == "users.info":
            return {"user": {"profile": {"display_name": "Amy"}}}
        if method == "auth.test":
            return {"user_id": "U1"}
        if method == "usergroups.list":
            return {"usergroups": []}
        raise AssertionError(f"unexpected slack call: {method}")

    async def _no_brief(*a, **k):
        return None

    monkeypatch.setattr(slack_src, "_call", fake_call)
    monkeypatch.setattr(slack_src, "_maybe_brief", _no_brief)  # no claude subprocess
    monkeypatch.setattr(slack_src, "_me_cache", None)
    monkeypatch.setattr(slack_src, "_subteams_cache", None)
    monkeypatch.setattr(slack_src, "_channels_cache", None)
    monkeypatch.setattr(slack_src, "_user_names", {})
    monkeypatch.setattr(settings, "slack_token", "xoxp-test")
    monkeypatch.setattr(settings, "slack_user_id", "U1")
    monkeypatch.setattr(settings, "slack_dms", False)
    monkeypatch.setattr(settings, "slack_broadcasts", False)
    assert await slack_src.poll() == 1
    async with maker() as s:
        card = (
            await s.execute(select(Card).where(Card.origin == "slack"))
        ).scalar_one()
    sl = card.cached["slack"]
    assert set(sl) == SLACK_KEYS  # FROZEN — recompute_ball + FE read these
    assert sl["unread"] is True and sl["kind"] == "mention"
    assert sl["channel"] == "C1" and sl["ts"] == now_ts
    assert sl["text"] == "<@U1> please review the export" and sl["from"] == "Amy"


# ── 2. /api/status poller-name identity across the M5 move ──────────────────────

# The pre-M5 poller name → expected-interval contract, hardcoded from the map.
# These names are FROZEN: /api/status consumers + the FE health card key on them.
LEGACY_POLLERS = {
    "agent-state": "agent_poll_s",
    "agent-state-panes": "agent_poll_s",
    "jira": "poll_interval_s",
    "github": "poll_interval_s",
    "github-pr-enrich": "poll_interval_s",
    "github-pr-enrich-manual": "poll_interval_s",
    "jira-links": "poll_interval_s",
    "link-enrich": "poll_interval_s",
    "slack": "poll_interval_s",
    "github-pr-refresh": "dates_interval_s",
    "done-prune": "dates_interval_s",
    "tmux-reap": "dates_interval_s",
    # ttyd-reap became a one-shot boot sweep (#36): it advertises no cadence
    # (interval_s=null) so the FE health probe skips age-based staleness for it.
    "ttyd-reap": None,
    "github-pr-brief": "dates_interval_s",
    "jira-dates": "dates_interval_s",
}


async def test_status_poller_names_identical_after_m5(client):
    for name in LEGACY_POLLERS:
        status.record_ok(name)
    got = (await client.get("/api/status")).json()["pollers"]
    for name, field in LEGACY_POLLERS.items():
        assert name in got, f"legacy poller {name!r} vanished from /api/status"
        expected = getattr(settings, field) if field else None
        assert got[name]["interval_s"] == expected, name
    # equality, not just subset: the set of names main.py declares a cadence for
    # (dotless = non-plugin) is EXACTLY the legacy set — nothing renamed, nothing added
    declared = {n for n, m in got.items() if "." not in n and m["interval_s"] is not None}
    assert declared == {n for n, f in LEGACY_POLLERS.items() if f is not None}
    # the composite source loops emit NO aggregate rows of their own: run_step
    # swallows step errors, so "src-jira.poll" etc. would be tautologically green —
    # only the legacy substep names above carry signal (PollSpec.report_status=False)
    assert not {n for n in got if n.startswith("src-")}
    # and even a stray record under a silent name gets no cadence stamped
    status.record_ok("src-jira.poll")
    got = (await client.get("/api/status")).json()["pollers"]
    assert got["src-jira.poll"]["interval_s"] is None


def test_source_cadences_served_by_kernel_pollspecs():
    """The loops themselves now ride the kernel: every source cadence is a
    discovered plugin PollSpec enumerated for lifespan (main.py imports no
    sources — see test below)."""
    polls = {n: i for n, _f, i in main_mod._plugin_polls()}
    assert polls["src-jira.poll"] == float(settings.poll_interval_s)
    assert polls["src-jira.links"] == float(settings.poll_interval_s)
    assert polls["src-jira.dates"] == float(settings.dates_interval_s)
    assert polls["src-github.aux"] == float(settings.poll_interval_s)
    assert polls["src-github.slow"] == float(settings.dates_interval_s)
    assert polls["src-slack.poll"] == float(settings.poll_interval_s)


def test_main_never_imports_a_named_source_plugin():
    """Disabling a source plugin must leave core booting — which requires main.py
    to depend on the GENERIC discovery/PLUGIN_ROUTERS surface only, never a
    named source plugin module directly (that would hardcode exactly the
    dependency disabling is supposed to remove).

    (conductor.sources itself was removed outright post-M8 — there is no
    longer a legacy dotted path to assert main.py avoids; the meaningful
    invariant left to pin is that main.py never names src_jira/src_github/
    src_slack, or any other plugins.src_* module, by import.)"""
    import conductor.main as m

    lines = [ln.strip() for ln in open(m.__file__)]
    offenders = [
        ln for ln in lines
        if ln.startswith(("from .plugins.src_", "from conductor.plugins.src_",
                          "import conductor.plugins.src_"))
    ]
    assert not offenders, offenders


def test_source_plugins_are_discovery_managed():
    """The four sources are ordinary discovered plugins — the plugins.json
    disable switch (discovery skip) therefore covers them for free."""
    for pid in ("src-jira", "src-github", "src-slack"):
        assert pid in PLUGINS and PLUGINS[pid].polls, pid


# ── 3. NEW capability: a kernel-registered plugin (no dataclass) IS a source ────


async def test_kernel_native_source_plugin_end_to_end(client, maker):
    """Register a poll straight into the kernel's registry — no Plugin dataclass,
    no discovery, no PLUGINS entry. Its poll is enumerated for lifespan and
    /api/status like any source, and one poll tick writes a card in the plugin's
    own cached namespace using the signal vocabulary → the card lands in the
    need_human lane. A plugin can BE a source through kernel surfaces alone."""
    assert "zz-kernsrc" not in PLUGINS  # genuinely kernel-native

    async def tick() -> None:
        async with maker() as s:
            await store.upsert_card(
                s, origin="zz-kernsrc", external_id="K-1", title="kernel-native thing",
                cached_patch={"zz-kernsrc": {"signal": {"state": "needs_me",
                                                        "detail": "approve the batch"}}},
            )

    effect = runtime.spec_registry("polls").register(
        runtime.SpecRegistration(
            plugin_id="zz-kernsrc", field="polls",
            specs=(PollSpec(name="sync", fn=tick, interval=12.0),),
        )
    )
    try:
        polls = {n: (f, i) for n, f, i in main_mod._plugin_polls()}
        assert "zz-kernsrc.sync" in polls and polls["zz-kernsrc.sync"][1] == 12.0
        await polls["zz-kernsrc.sync"][0]()  # one tick, exactly as _plugin_poll_loop runs it
        status.record_ok("zz-kernsrc.sync")  # what a completed loop run records

        cards = (await client.get("/api/cards")).json()
        card = next(c for c in cards if c["external_id"] == "K-1")
        assert card["origin"] == "zz-kernsrc"
        assert card["ball"] == "human" and card["lane"] == "need_human"
        assert card["agent_state"] == "approve the batch"

        pollers = (await client.get("/api/status")).json()["pollers"]
        assert pollers["zz-kernsrc.sync"]["interval_s"] == 12.0
    finally:
        effect.dispose()
    main_mod._PLUGIN_POLLS = None
    assert "zz-kernsrc.sync" not in {n for n, _f, _i in main_mod._plugin_polls()}
