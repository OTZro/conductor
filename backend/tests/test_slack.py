"""Unit tests for the Slack source (no network)."""

import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from conductor.models import Card, utcnow
from conductor.plugins.src_slack import slack


def test_recent_true_for_now():
    assert slack._recent(str(time.time())) is True


def test_recent_false_for_old_and_bad():
    assert slack._recent("1") is False  # 1970
    assert slack._recent(None) is False
    assert slack._recent("not-a-ts") is False


def test_ts_newer_only_on_strictly_newer():
    # a genuinely newer message resurfaces a Done card; the steady re-poll (same ts)
    # must NOT, or a done thread would never stay done.
    assert slack._ts_newer("1783461625.1", "1783461625.0") is True
    assert slack._ts_newer("1783461625.0", "1783461625.0") is False
    assert slack._ts_newer("100", "200") is False
    assert slack._ts_newer(None, "100") is False
    assert slack._ts_newer("100", None) is False


@pytest.mark.asyncio
async def test_poll_disabled_without_token(monkeypatch):
    monkeypatch.setattr(slack.settings, "slack_token", None)
    assert await slack.poll() == 0


# --- the mention keep window (CONDUCTOR_SLACK_MENTION_KEEP_DAYS) ------------------
# An un-actioned mention is kept past the 7-day surfacing window on purpose, but the
# hatch used to be unbounded and nothing else could reach these cards (prune_done only
# touches ball == "none"; an unread mention is ball == "human"), so they piled up
# forever. These pin both halves: still kept while fresh, finally dropped once stale.


_OMITTED = object()  # NOT None — None is itself a ts value under test (see below)


def _mention(days_old: float, *, done: bool = False, ts: object = _OMITTED) -> Card:
    """Omit `ts` for a real timestamp `days_old` days back; pass it explicitly to build
    a card with a bad one. The sentinel cannot be None: it used to be, so `ts=None`
    silently produced a fresh VALID timestamp and the missing-ts case was untestable."""
    stamp = str(time.time() - days_old * 86400) if ts is _OMITTED else ts
    return Card(
        origin="slack",
        external_id="mention:C1:1.2",
        title="ping",
        ball="human",
        cached={"slack": {"ts": stamp, "unread": True, **({"done": True} if done else {})}},
    )


def test_unactioned_mention_kept_inside_the_window(monkeypatch):
    monkeypatch.setattr(slack.settings, "slack_mention_keep_days", 14)
    # past the 7-day SURFACING window but inside the keep window — the whole point of
    # the hatch: it dropped out of search, it must not drop off the board
    assert slack._keep_slack(_mention(10), None) is True


def test_unactioned_mention_pruned_past_the_window(monkeypatch):
    monkeypatch.setattr(slack.settings, "slack_mention_keep_days", 14)
    assert slack._keep_slack(_mention(20), None) is False


def test_zero_days_keeps_mentions_forever(monkeypatch):
    """0 = opt out of expiry entirely (the old unbounded behavior)."""
    monkeypatch.setattr(slack.settings, "slack_mention_keep_days", 0)
    assert slack._keep_slack(_mention(999), None) is True


def test_a_negative_window_also_keeps_mentions_forever(monkeypatch):
    """The guard is `<= 0`, not `== 0`. A negative window read literally would mean
    "expire everything", which is the opposite of what someone typing -1 to switch the
    feature off is asking for — so it disables expiry like 0 does. (PR review)"""
    monkeypatch.setattr(slack.settings, "slack_mention_keep_days", -1)
    assert slack._keep_slack(_mention(999), None) is True


def test_expiry_never_drops_a_card_on_a_bad_ts(monkeypatch):
    """Fail safe: an absent/garbage ts keeps the card. Losing a genuine ask to a
    parsing failure is far worse than carrying one card too long.

    "-inf"/"inf"/"nan" are here because float() ACCEPTS them: they parse, so they never
    reach the except branch. Only "-inf" ever bit (age becomes +inf → pruned), but all
    three are pinned so the fail-safe holds by construction. (PR review)"""
    monkeypatch.setattr(slack.settings, "slack_mention_keep_days", 14)
    for bad in (None, "", "not-a-ts", "-inf", "inf", "nan"):
        assert slack._keep_slack(_mention(0, ts=bad), None) is True, f"pruned on ts={bad!r}"
    # the ts key absent entirely, not merely None — .get() returns None either way, but
    # only this shape proves the lookup itself is safe
    no_ts = Card(origin="slack", external_id="mention:C1:1.2", title="ping", ball="human",
                 cached={"slack": {"unread": True}})
    assert slack._keep_slack(no_ts, None) is True


def _ls(*, dismissed=False, pinned=False, snoozed_until=None) -> SimpleNamespace:
    """A LocalState stand-in. Spells out every field _user_held reads, so adding a
    flag there fails loudly here instead of silently defaulting to "not held"."""
    return SimpleNamespace(dismissed=dismissed, pinned=pinned, snoozed_until=snoozed_until)


def test_stale_mention_still_kept_once_actioned(monkeypatch):
    """dismissed/done resolutions outrank the age bound — they're the record that the
    ask was handled, and re-pruning them would let the next poll resurface the ask."""
    monkeypatch.setattr(slack.settings, "slack_mention_keep_days", 14)
    assert slack._keep_slack(_mention(99, done=True), None) is True
    assert slack._keep_slack(_mention(99), _ls(dismissed=True)) is True


def test_stale_mention_kept_while_pinned_or_snoozed(monkeypatch):
    """A pin or a live snooze is the user saying "I've claimed this" just as much as a
    dismiss, and prune goes through delete_card_cascade — the LocalState (pin, snooze,
    note, picked option) dies with the card and no later poll restores it, because a
    card past the bound is outside the surfacing window.

    Snooze is the silent one: api/cards.py hides a snoozed card until it fires, so
    pruning one mid-snooze loses the ask with nothing on the board to notice. Bounding
    the mention hatch is what put these in reach at all — while it was unbounded no
    mention could be pruned, so `dismissed` alone sufficed. (PR review)"""
    monkeypatch.setattr(slack.settings, "slack_mention_keep_days", 14)
    assert slack._keep_slack(_mention(99), _ls(pinned=True)) is True
    assert slack._keep_slack(
        _mention(99), _ls(snoozed_until=utcnow() + timedelta(days=7))
    ) is True


def test_stale_mention_prunes_once_the_snooze_has_fired(monkeypatch):
    """The other half: snooze defers the bound, it does not cancel it. Once the snooze
    fires the card is back on the board, visible, and ages out normally — otherwise
    one snooze would silently restore the unbounded hatch this PR removed."""
    monkeypatch.setattr(slack.settings, "slack_mention_keep_days", 14)
    fired = _ls(snoozed_until=utcnow() - timedelta(seconds=1))
    assert slack._keep_slack(_mention(99), fired) is False
    assert slack._keep_slack(_mention(1), fired) is True  # still inside the window


def test_pinned_dm_is_kept_too(monkeypatch):
    """The hatch is per-card intent, not per-kind: a DM the user pinned is a claim on
    that card, so absence from the search window must not delete it either."""
    monkeypatch.setattr(slack.settings, "slack_mention_keep_days", 0)
    dm = Card(origin="slack", external_id="dm:D1:1.2", title="dm", ball="human",
              cached={"slack": {"ts": str(time.time())}})
    assert slack._keep_slack(dm, _ls(pinned=True)) is True
    assert slack._keep_slack(dm, _ls()) is False


def test_dm_still_prunes_on_absence(monkeypatch):
    """The hatch was only ever about mentions; a DM that left the window still goes."""
    monkeypatch.setattr(slack.settings, "slack_mention_keep_days", 0)
    dm = Card(origin="slack", external_id="dm:D1:1.2", title="dm", ball="human",
              cached={"slack": {"ts": str(time.time())}})
    assert slack._keep_slack(dm, None) is False


def test_mine_filters_usergroups_by_membership():
    groups = [
        {"id": "S1", "handle": "develop", "users": ["U_ME", "U_OTHER"]},
        {"id": "S2", "handle": "design", "users": ["U_OTHER"]},
        {"id": "S3", "name": "QA", "users": ["U_ME"]},  # no handle → fall back to name
        {"id": "S4", "handle": "empty"},  # no users key at all
    ]
    assert slack._mine("U_ME", groups) == [("S1", "develop"), ("S3", "QA")]


# ── _handle_match filter chain ───────────────────────────────────────────────
# Each filter here is a shipped incident: 26 of the first 33 broadcast cards were
# word-match false positives (query <!here> matches the literal word "here");
# team-mazu's @channel was noise while direct @me had to survive; feed-* group
# pings flooded the board.

import time as _time

import pytest

from conductor.plugins.src_slack import slack as slack_mod


def _match(text: str, *, chan_id="C1", chan_name="general", ts=None, permalink=None) -> dict:
    return {
        "ts": ts or str(_time.time()),
        "channel": {"id": chan_id, "name": chan_name},
        "permalink": permalink,
        "text": text,
    }


@pytest.fixture
def captured(monkeypatch):
    """Silence side effects; capture what would become a card."""
    out: list[dict] = []

    async def fake_upsert(external_id, title, url, kind, channel, ts, text, sender=None):
        out.append({"external_id": external_id, "title": title, "text": text, "sender": sender})

    async def fake_brief(external_id, text):
        return None

    monkeypatch.setattr(slack_mod, "_upsert", fake_upsert)
    monkeypatch.setattr(slack_mod, "_maybe_brief", fake_brief)
    monkeypatch.setattr(slack_mod.settings, "slack_exclude_channels", "release_mgmt")
    monkeypatch.setattr(slack_mod.settings, "slack_broadcast_exclude_channels", "team-mazu")
    monkeypatch.setattr(slack_mod.settings, "slack_direct_only_prefixes", "feed-")
    return out


@pytest.mark.asyncio
async def test_broadcast_wordmatch_false_positive_dropped(captured):
    """search.messages word-matches: '<!here>' also hits the literal word 'Here'.
    Without the raw markup token in the body it is NOT a real broadcast."""
    m = _match("They updated their IP address. Here is the updated list")
    n = await slack_mod._handle_match(m, "<!here>", "@here", set(), True, {"C1"}, False)
    assert n == 0 and captured == []


@pytest.mark.asyncio
async def test_broadcast_with_real_markup_kept(captured):
    m = _match("<!channel> 大家早安,今日午餐請填表單")
    n = await slack_mod._handle_match(m, "<!channel>", "@channel", set(), True, {"C1"}, False)
    assert n == 1 and len(captured) == 1


@pytest.mark.asyncio
async def test_broadcast_excluded_channel_drops_but_direct_survives(captured):
    """team-mazu: @channel is noise, a direct @me still counts."""
    bc = _match("<!channel> weekly report", chan_name="team-mazu")
    n = await slack_mod._handle_match(bc, "<!channel>", "@channel", set(), True, {"C1"}, False)
    assert n == 0
    direct = _match("<@US6T9ELK0> 幫我看一下這個", chan_name="team-mazu")
    n = await slack_mod._handle_match(direct, "<@US6T9ELK0>", "@you", set(), False, set(), True)
    assert n == 1


@pytest.mark.asyncio
async def test_feed_channel_group_ping_dropped_direct_kept(captured):
    group = _match("<@S123> new build", chan_name="feed-ci")
    n = await slack_mod._handle_match(group, "<@S123>", "@rd", set(), False, set(), False)
    assert n == 0
    direct = _match("<@US6T9ELK0> deploy?", chan_name="feed-ci")
    n = await slack_mod._handle_match(direct, "<@US6T9ELK0>", "@you", set(), False, set(), True)
    assert n == 1


@pytest.mark.asyncio
async def test_release_mgmt_is_direct_only(captured, monkeypatch):
    """#release_mgmt: only a direct @me becomes a job; @usergroup/@channel pings there
    are dropped (was fully excluded before)."""
    monkeypatch.setattr(slack_mod.settings, "slack_exclude_channels", "")
    monkeypatch.setattr(slack_mod.settings, "slack_direct_only_prefixes", "feed-,release_mgmt")
    direct = _match("<@US6T9ELK0> 幫我看", chan_name="release_mgmt")
    assert await slack_mod._handle_match(direct, "<@US6T9ELK0>", "@you", set(), False, set(), True) == 1
    group = _match("<!subteam^S1> ping", chan_name="release_mgmt")
    assert await slack_mod._handle_match(group, "<@S1>", "@develop", set(), False, set(), False) == 0


@pytest.mark.asyncio
async def test_excluded_channel_always_dropped(captured):
    m = _match("<@US6T9ELK0> ping", chan_name="release_mgmt")
    n = await slack_mod._handle_match(m, "<@US6T9ELK0>", "@you", set(), False, set(), True)
    assert n == 0 and captured == []


@pytest.mark.asyncio
async def test_nonmember_broadcast_dropped(captured):
    """A @channel I can search but in a channel I'm not a member of never pinged me."""
    m = _match("<!channel> hello", chan_id="C_NOT_MINE")
    n = await slack_mod._handle_match(m, "<!channel>", "@channel", set(), True, {"C1"}, False)
    assert n == 0


@pytest.mark.asyncio
async def test_same_thread_collapses_to_one_card(captured):
    seen: set[str] = set()
    link = "https://x.slack.com/archives/C1/p1?thread_ts=111.222"
    m1 = _match("<@US6T9ELK0> q1", permalink=link)
    m2 = _match("<@US6T9ELK0> q2", permalink=link)
    n1 = await slack_mod._handle_match(m1, "<@US6T9ELK0>", "@you", seen, False, set(), True)
    n2 = await slack_mod._handle_match(m2, "<@US6T9ELK0>", "@you", seen, False, set(), True)
    assert (n1, n2) == (1, 0) and len(captured) == 1


# ── usergroup pings: the 2026-08 flood ───────────────────────────────────────
# Slack search stopped tolerating the `<!subteam^S…>` markup the Web API documents:
# the query became a fuzzy match over the whole workspace (1.7M hits, none of which
# mentioned the group) and 25 of 27 cards in 15h were lunch chatter, app notifications
# and deal alerts. Two defences: query the form that really appears in a message body,
# and verify every hit carries it.


@pytest.mark.asyncio
async def test_group_target_queries_the_at_form_not_subteam_markup(monkeypatch):
    """`<!subteam^S…>` is unsearchable — production must ask for `<@S…>`."""
    asked: list[str] = []

    async def fake_subteams(token, uid):
        return [("S123", "rd-team")]

    accepts: list = []

    async def fake_search(token, query, label, seen, member_only, my_channels, direct, accept=None):
        asked.append(query)
        accepts.append(accept)
        return 0

    monkeypatch.setattr(slack_mod, "_my_subteams", fake_subteams)
    monkeypatch.setattr(slack_mod, "_search", fake_search)
    monkeypatch.setattr(slack_mod.settings, "slack_broadcasts", False)
    await slack_mod._poll_mentions("tok", "US6T9ELK0", set())
    assert asked == ["<@US6T9ELK0>", "<@S123>"]
    assert accepts == [("<@US6T9ELK0",), ("<@S123", "<!subteam^S123")]


@pytest.mark.asyncio
async def test_group_hit_without_the_group_mention_dropped(captured):
    """The flood itself: a hit search returned for a group query that never pinged it."""
    m = _match("ㄟ…好像哪裡怪怪的…", chan_name="gf-ai")
    n = await slack_mod._handle_match(m, "<@S123>", "@rd-team", set(), False, set(), False)
    assert n == 0 and captured == []


@pytest.mark.asyncio
async def test_group_hit_with_the_group_mention_kept(captured):
    m = _match("<@S123> 想請大家幫看一個 CI 改動的風險", chan_name="help-develop")
    n = await slack_mod._handle_match(m, "<@S123>", "@rd-team", set(), False, set(), False)
    assert n == 1 and len(captured) == 1


@pytest.mark.asyncio
async def test_group_hit_accepts_the_documented_subteam_form_too(captured):
    """A body may carry `<!subteam^S…|@handle>` (the documented markup) while the SEARCH
    asked for `<@S…>` — verification accepts every genuine form of the id, not just the
    one the query happened to use."""
    m = _match("<!subteam^S123|@rd-team> 大家幫看一下這個 incident", chan_name="help-develop")
    n = await slack_mod._handle_match(
        m, "<@S123>", "@rd-team", set(), False, set(), False,
        accept=("<@S123", "<!subteam^S123"),
    )
    assert n == 1 and len(captured) == 1


@pytest.mark.asyncio
async def test_app_notification_dropped_for_group_ping_but_direct_kept(captured):
    """jira / google_calendar / sentry posts swept up by a group ping are not asks;
    an app that addresses me directly still is."""
    bot = _match("<@S123> Daily agenda", chan_name="general") | {"bot_id": "B1"}
    assert await slack_mod._handle_match(bot, "<@S123>", "@rd-team", set(), False, set(), False) == 0
    app = _match("<@US6T9ELK0> assigned you PROJ-1", chan_name="general") | {"app_id": "A1"}
    assert await slack_mod._handle_match(app, "<@US6T9ELK0>", "@you", set(), False, set(), True) == 1


# ── the three env knobs ──────────────────────────────────────────────────────
# Policy (which groups count, whether bots count) is per-person config. The query
# TEMPLATE is not a preference — it is the escape hatch for the next time Slack
# changes its search behaviour, and it stays safe because the verification above
# derives what to check from whatever the template rendered.


@pytest.mark.asyncio
async def test_query_template_override_is_used_for_both_targets(monkeypatch):
    asked: list[str] = []

    async def fake_subteams(token, uid):
        return [("S123", "rd-team")]

    async def fake_search(token, query, label, seen, member_only, my_channels, direct, accept=None):
        asked.append(query)
        return 0

    monkeypatch.setattr(slack_mod, "_my_subteams", fake_subteams)
    monkeypatch.setattr(slack_mod, "_search", fake_search)
    monkeypatch.setattr(slack_mod.settings, "slack_broadcasts", False)
    monkeypatch.setattr(slack_mod.settings, "slack_mention_query_template", "<!subteam^{id}>")
    await slack_mod._poll_mentions("tok", "US6T9ELK0", set())
    assert asked == ["<!subteam^US6T9ELK0>", "<!subteam^S123>"]


@pytest.mark.asyncio
async def test_verification_follows_the_template(captured, monkeypatch):
    """A custom template is still verified — an override can shrink the catch, never flood."""
    monkeypatch.setattr(slack_mod.settings, "slack_mention_query_template", "<!subteam^{id}>")
    hit = _match("<!subteam^S123> please review", chan_name="help-develop")
    assert await slack_mod._handle_match(hit, "<!subteam^S123>", "@rd", set(), False, set(), False) == 1
    miss = _match("lunch is here", chan_name="help-develop")
    assert await slack_mod._handle_match(miss, "<!subteam^S123>", "@rd", set(), False, set(), False) == 0


@pytest.mark.asyncio
async def test_include_usergroups_is_an_allowlist(monkeypatch):
    async def fake_call(method, token, params):
        return {
            "usergroups": [
                {"id": "S1", "handle": "rd-team", "users": ["U1"]},
                {"id": "S2", "handle": "everyday-poke", "users": ["U1"]},
                {"id": "S3", "handle": "release-management", "users": ["U1"]},
            ]
        }

    monkeypatch.setattr(slack_mod, "_call", fake_call)
    monkeypatch.setattr(slack_mod, "_subteams_cache", None)
    monkeypatch.setattr(slack_mod.settings, "slack_exclude_usergroups", "release-management")

    monkeypatch.setattr(slack_mod.settings, "slack_include_usergroups", "")
    assert [h for _, h in await slack_mod._my_subteams("tok", "U1")] == ["rd-team", "everyday-poke"]

    monkeypatch.setattr(slack_mod, "_subteams_cache", None)
    monkeypatch.setattr(slack_mod.settings, "slack_include_usergroups", "@rd-team")
    assert [h for _, h in await slack_mod._my_subteams("tok", "U1")] == ["rd-team"]


@pytest.mark.asyncio
async def test_allow_bot_mentions_opts_app_posts_back_in(captured, monkeypatch):
    bot = _match("<@S123> Daily agenda", chan_name="general") | {"bot_id": "B1"}
    assert await slack_mod._handle_match(bot, "<@S123>", "@rd", set(), False, set(), False) == 0
    monkeypatch.setattr(slack_mod.settings, "slack_allow_bot_mentions", True)
    assert await slack_mod._handle_match(bot, "<@S123>", "@rd", set(), False, set(), False) == 1

