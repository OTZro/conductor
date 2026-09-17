from __future__ import annotations

import logging

import asyncio
import math
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select

from ... import prompts, store
from ...config import settings
from ...db import session_maker
from ...models import Card, utcnow

log = logging.getLogger("conductor.slack")

_API = "https://slack.com/api"
# Only RECENT messages become a job (don't trawl ancient history — the window is
# CONDUCTOR_SLACK_MAX_AGE_DAYS). Once carded, a mention persists regardless of age.

# Accepts either a USER token (xoxp-…, scopes im:read/im:history/search:read) OR a
# browser session token (xoxc-… + the `d` cookie = xoxd-…, what the slack-mcp setup
# uses). Set CONDUCTOR_SLACK_TOKEN (+ CONDUCTOR_SLACK_COOKIE for xoxc) +
# CONDUCTOR_SLACK_USER_ID to enable; absent ⇒ disabled.

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


async def _call(method: str, token: str, params: dict) -> dict:
    # token in the form body works for both xoxp and xoxc; xoxc additionally needs
    # the `d` cookie and a browser-like UA.
    headers = {"User-Agent": _UA}
    if settings.slack_cookie:
        headers["Cookie"] = f"d={settings.slack_cookie}"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(f"{_API}/{method}", data={**params, "token": token}, headers=headers)
        data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"slack {method}: {data.get('error')}")
    return data


def _recent(ts: str | None) -> bool:
    try:
        return (time.time() - float(ts)) < settings.slack_max_age_days * 86400
    except (TypeError, ValueError):
        return False


async def _permalink(token: str, channel: str, ts: str) -> str | None:
    try:
        d = await _call("chat.getPermalink", token, {"channel": channel, "message_ts": ts})
        return d.get("permalink")
    except Exception:  # noqa: BLE001
        return None


def _thread_key(channel: str | None, ts: str | None, permalink: str | None) -> str:
    """One card per THREAD: several @-mentions in the same thread collapse to one.

    The fallback MUST carry the channel too: permalink fetches flake, and a key whose
    shape depends on that fetch mints a second card for the same message on the next
    poll (bitten 2026-09-02: one DM → two mention cards, 4 minutes apart). For a
    top-level message thread_ts == ts, so both paths now yield the same key."""
    if permalink:
        tt = parse_qs(urlparse(permalink).query).get("thread_ts", [None])[0]
        if tt:
            return f"mention:{channel}:{tt}"
    return f"mention:{channel}:{ts}" if channel else f"mention:{ts}"


_user_names: dict[str, str] = {}


async def _user_name(token: str, uid: str | None) -> str | None:
    """Display name for a Slack user id (cached — names rarely change), so a card can
    show WHO wrote the message instead of only the channel/ts key."""
    if not uid:
        return None
    if uid in _user_names:
        return _user_names[uid]
    try:
        d = await _call("users.info", token, {"user": uid})
    except Exception as exc:  # noqa: BLE001
        log.warning("[users.info %s] %s", uid, exc)
        return None
    u = d.get("user") or {}
    prof = u.get("profile") or {}
    name = prof.get("display_name") or prof.get("real_name") or u.get("real_name") or u.get("name")
    if name:
        _user_names[uid] = name
    return name


_me_cache: tuple[str | None, str | None] | None = None


async def _me(token: str) -> tuple[str | None, str | None]:
    """(user_id, display_name) for the account this token belongs to — derived once via
    auth.test so a new user only pastes a Slack token (no manual uid/name lookup). An
    explicit CONDUCTOR_SLACK_USER_ID still wins."""
    global _me_cache
    if _me_cache is not None:
        return _me_cache
    uid = settings.slack_user_id
    name = None
    if token:
        try:
            d = await _call("auth.test", token, {})
            uid = uid or d.get("user_id")
        except Exception as exc:  # noqa: BLE001
            log.warning("[auth.test] %s", exc)
        if uid:
            name = await _user_name(token, uid)
    _me_cache = (uid, name)
    return _me_cache


# --- claude brief: a one-line "what is being asked of me" per mention ----------
_brief_sem = asyncio.Semaphore(4)
_briefing: set[str] = set()


async def _brief(text: str, threaded: bool = False) -> str | None:
    _, name = await _me(settings.slack_token)
    who = f" ({name})" if name else ""  # e.g. "@me (Alex)"; drops the name if unknown
    # template from prompts.py (user-overridable); the message text is appended after
    # it, then the non-overridable no-tools guard (see prompts.NO_TOOLS_GUARD)
    prompt = (
        prompts.prompt("slack_brief_thread" if threaded else "slack_brief_single", who=who)
        + "\n\n"
        + text[: 3000 if threaded else 1500]
        + prompts.NO_TOOLS_GUARD
    )
    proc = await asyncio.create_subprocess_exec(
        # --strict-mcp-config: skip every MCP server; a summary needs no tools and
        # MCP startup dominated the latency AND token bill of each brief.
        # --disallowedTools: block the built-ins too — a mentioned ticket key once
        # tempted the model into an acli lookup whose permission denial became the brief.
        settings.claude_bin, "-p", prompt,
        "--model", settings.brief_model, "--strict-mcp-config",
        "--disallowedTools", "*",  # deny-all: a name list rots as the CLI grows tools
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        cwd=str(Path.home()),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=90)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0:
        return None
    return out.decode().strip()[:400] or None


async def _maybe_brief(external_id: str, text: str) -> None:
    """Brief once per card; skip if already briefed or a brief is in flight. Feeds
    the WHOLE thread (oldest→newest, sender-tagged) to the summarizer, not just the
    one @-message — a terse reply deep in a thread ("can you check this?") only makes
    sense with the discussion above it. Falls back to the single message for DMs or
    an unthreaded ping."""
    if external_id in _briefing:
        return
    async with session_maker() as session:
        card = (
            await session.execute(
                select(Card).where(Card.origin == "slack", Card.external_id == external_id)
            )
        ).scalar_one_or_none()
    if not card or ((card.cached or {}).get("slack") or {}).get("brief"):
        return
    _briefing.add(external_id)
    try:
        thread = await fetch_thread(card)  # one conversations.replies call, once per card
        if len(thread) > 1:
            convo = "\n".join(
                f"{m['user']}{' (me)' if m['is_me'] else ''}: {m['text']}" for m in thread
            )
            source, threaded = convo[-3000:], True  # keep the tail: recent turns + the @
        else:
            source, threaded = text, False
        async with _brief_sem:
            brief = await _brief(source, threaded=threaded)
        if brief:
            async with session_maker() as session:
                await store.upsert_card(
                    session, origin="slack", external_id=external_id,
                    cached_patch={"slack": {"brief": brief}}, create=False,
                )
    finally:
        _briefing.discard(external_id)


def _ts_newer(a, b) -> bool:
    """Is slack ts `a` strictly newer than `b`? (ts is epoch-seconds as a string.)"""
    try:
        return float(a) > float(b)
    except (TypeError, ValueError):
        return False


async def _upsert(external_id, title, url, kind, channel, ts, text, sender=None) -> None:
    extra = [{"kind": "slack", "ref": url, "url": url, "title": "slack", "auto": True}] if url else None
    slack = {"unread": True, "kind": kind, "channel": channel, "ts": ts, "text": text}
    if sender:
        slack["from"] = sender  # who wrote it, for the card to show
    async with session_maker() as session:
        # a new message in a thread/DM you'd marked Done pulls it back to Need Human:
        # clear the done flag when THIS message is strictly newer than the one on
        # record (the poll re-sees the same message every cycle — same ts — so only a
        # genuinely newer ts resurfaces it, not the steady re-poll).
        existing = (
            await session.execute(
                select(Card).where(Card.origin == "slack", Card.external_id == external_id)
            )
        ).scalar_one_or_none()
        prev = ((existing.cached if existing else None) or {}).get("slack") or {}
        if prev.get("done") and _ts_newer(ts, prev.get("ts")):
            slack["done"] = False
        await store.upsert_card(
            session,
            origin="slack",
            external_id=external_id,
            title=title,
            summary=text or "",
            url=url,
            cached_patch={"slack": slack},
            link_text=text,
            extra_links=extra,
        )


async def _list_all_ims(token: str) -> list[dict]:
    """Every DM conversation, following pagination. conversations.list is NOT sorted
    by recency, so a fixed head-slice silently drops recently-active DMs sitting on a
    later page — that's how a just-received DM never became a job (100 DMs, the poll
    only looked at the first 50)."""
    out: list[dict] = []
    cursor = ""
    for _ in range(10):  # safety bound (~2000 DMs)
        params = {"types": "im", "limit": "200", "exclude_archived": "true"}
        if cursor:
            params["cursor"] = cursor
        d = await _call("conversations.list", token, params)
        out += d.get("channels", [])
        cursor = (d.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
    return out


async def _poll_dms(token: str, uid: str | None, seen: set[str]) -> int:
    ims = await _list_all_ims(token)
    cids = [im["id"] for im in ims if im.get("id")]

    # fetch each DM's latest message concurrently (bounded — _call has no 429 retry,
    # so a big burst would trip rate limits) and tolerate per-DM failures so one bad
    # history call doesn't sink the whole poll; it retries next cycle.
    sem = asyncio.Semaphore(5)

    async def _last(cid: str) -> dict | None:
        async with sem:
            try:
                hist = await _call("conversations.history", token, {"channel": cid, "limit": "1"})
            except Exception as exc:  # noqa: BLE001
                log.warning("[dm history %s] %s", cid, exc)
                return None
        msgs = hist.get("messages") or []
        return msgs[0] if msgs else None

    latest = await asyncio.gather(*(_last(cid) for cid in cids))

    count = 0
    for cid, m in zip(cids, latest):
        if not m:
            continue
        if m.get("bot_id") or m.get("app_id") or m.get("subtype") == "bot_message":
            continue  # app/bot notification (Google Meet, calendar…), not a human DM
        ts = m.get("ts")
        if not _recent(ts):
            continue  # only recent DM activity becomes a job
        if uid and m.get("user") == uid:
            continue  # my own last message — nothing waiting on me
        text = (m.get("text") or "")[:4000]
        url = await _permalink(token, cid, ts)
        sender = await _user_name(token, m.get("user"))
        ext = f"dm:{cid}"
        # title is WHO sent it (not the raw message text); the text drives the AI
        # brief + summary instead.
        await _upsert(ext, f"DM · {sender or 'someone'}", url, "dm", cid, ts, text, sender=sender)
        seen.add(ext)
        asyncio.create_task(_maybe_brief(ext, text))  # one-line "what they want", like mentions
        count += 1
    return count


def _mine(uid: str, usergroups: list[dict]) -> list[tuple[str, str]]:
    """Usergroups (id, handle) that list `uid` as a member."""
    return [
        (g["id"], g.get("handle") or g.get("name") or g["id"])
        for g in usergroups
        if uid in (g.get("users") or [])
    ]


_subteams_cache: tuple[float, list[tuple[str, str]]] | None = None


async def _my_subteams(token: str, uid: str) -> list[tuple[str, str]]:
    """The @usergroups I belong to, so a <!subteam^…|@develop> ping counts as
    'mentions me'. Cached 1h (membership rarely changes). Needs the usergroups:read
    scope — degrades to [] (direct + broadcast only) if the call fails."""
    global _subteams_cache
    now = time.time()
    if _subteams_cache and now - _subteams_cache[0] < 3600:
        return _subteams_cache[1]
    try:
        d = await _call("usergroups.list", token, {"include_users": "true"})
    except Exception as exc:  # noqa: BLE001
        log.warning("[usergroups] %s", exc)
        return _subteams_cache[1] if _subteams_cache else []
    include = settings.slack_included_groups  # empty = every group I'm in
    groups = [
        (gid, h)
        for gid, h in _mine(uid, d.get("usergroups", []))
        if h.lower() not in settings.slack_excluded_groups
        and (not include or h.lower() in include)
    ]
    _subteams_cache = (now, groups)
    return groups


_channels_cache: tuple[float, set[str]] | None = None


async def _my_channels(token: str) -> set[str]:
    """Channel IDs I'm a member of. A @channel/@here only pings me if I'm *in* the
    channel, so broadcasts are scoped to this set. Cached 1h."""
    global _channels_cache
    now = time.time()
    if _channels_cache and now - _channels_cache[0] < 3600:
        return _channels_cache[1]
    ids: set[str] = set()
    cursor = ""
    try:
        for _ in range(12):  # page cap (member of 150+ channels)
            params = {"types": "public_channel,private_channel", "limit": "1000", "exclude_archived": "true"}
            if cursor:
                params["cursor"] = cursor
            d = await _call("users.conversations", token, params)
            ids.update(c["id"] for c in d.get("channels", []) if c.get("id"))
            cursor = (d.get("response_metadata") or {}).get("next_cursor") or ""
            if not cursor:
                break
    except Exception as exc:  # noqa: BLE001
        log.warning("[channels] %s", exc)
        return _channels_cache[1] if _channels_cache else set()
    _channels_cache = (now, ids)
    return ids


async def _handle_match(
    m: dict, query: str, label: str, seen: set[str], member_only: bool,
    my_channels: set[str], direct: bool, token: str | None = None,
    accept: tuple[str, ...] | None = None,
) -> int:
    """Turn one search hit into a card (one per thread). `label` is the handle that
    pinged me (@you / @develop / @channel) and leads the card title. `member_only`
    (broadcasts) drops hits in channels I'm not in — a @channel I can search but
    don't belong to never actually pinged me."""
    ts = m.get("ts")
    if not _recent(ts):
        return 0  # only recent mentions become a job (old ones already carded persist)
    ch = m.get("channel") or {}
    if member_only and ch.get("id") not in my_channels:
        return 0
    # search.messages does NOT guarantee the hit contains the queried markup: a `<!here>`
    # query word-matches the literal word "here" (26 of the first 33 broadcast hits were
    # such false positives), and a query Slack cannot parse degrades into a fuzzy match
    # over the whole workspace (`<!subteam^S…>` returned 1.7M "matches", none of which
    # mentioned the group — that is how #gf-ai lunch chatter became a job). Only the raw
    # markup token in the message body proves this message really pinged me, so EVERY
    # target verifies, not just broadcasts.
    # `accept` decouples verification from the SEARCH string: a usergroup renders in a
    # message body as either `<@S…>` or the documented `<!subteam^S…|@handle>`, and the
    # query only ever carries one of those — deriving the check from the query alone
    # would reject the other, genuine, form. Callers without a mention id (broadcasts)
    # still verify against the query itself.
    raw = m.get("text") or ""
    tokens = accept or (query.rstrip(">"),)
    if not any(f"{t}>" in raw or f"{t}|" in raw for t in tokens):  # <!here> or <@U1|name>
        return 0
    if (
        not direct
        and not settings.slack_allow_bot_mentions
        and (m.get("bot_id") or m.get("app_id") or m.get("subtype") == "bot_message")
    ):
        # app/bot post (jira, google_calendar, sentry…) swept up by a group/broadcast
        # ping — not someone asking me for something. A bot that addresses me DIRECTLY
        # still counts, so `direct` is exempt from the setting too.
        return 0
    if ch.get("name") in settings.slack_excluded:
        return 0  # skip noisy channels entirely (e.g. release_mgmt)
    if member_only and ch.get("name") in settings.slack_broadcast_exclude:
        return 0  # this channel's @channel/@here/@everyone is noise; direct + @group still count
    if not direct and (ch.get("name") or "").startswith(settings.slack_direct_only):
        return 0  # feed-* firehoses: only a direct @me is a task, not @group/@channel
    if str(ch.get("id") or "").startswith("D"):
        # a DM channel: the DM poller owns it (one dm:<channel> card); carding the
        # @-mention TOO gives the same message twice under different origins.
        return 0
    url = m.get("permalink")
    key = _thread_key(ch.get("id"), ts, url)
    if key in seen:
        return 0  # this thread already captured (newest hit wins; sort=timestamp desc)
    seen.add(key)
    text = (m.get("text") or "")[:4000]
    sender = await _user_name(token, m.get("user"))
    await _upsert(
        key, f"{label} in #{ch.get('name', '?')} · {text[:50]}", url, "mention",
        ch.get("id"), ts, text, sender=sender,
    )
    asyncio.create_task(_maybe_brief(key, text))
    return 1


async def _search(
    token: str, query: str, label: str, seen: set[str], member_only: bool,
    my_channels: set[str], direct: bool, accept: tuple[str, ...] | None = None
) -> int:
    res = await _call("search.messages", token, {"query": query, "count": "30", "sort": "timestamp"})
    n = 0
    for m in res.get("messages", {}).get("matches", []):
        n += await _handle_match(m, query, label, seen, member_only, my_channels, direct, token, accept)
    return n


async def _poll_mentions(token: str, uid: str, seen: set[str]) -> int:
    """Everything that pings *me*: my direct @, the @usergroups I'm in (@develop…),
    and — when slack_broadcasts is on — @channel/@here/@everyone in channels I belong
    to. Each is its own search so one failing token (missing scope, unsupported
    query) doesn't sink the rest; direct @me is searched first so it wins the label
    when a thread matches several."""
    q = settings.slack_mention_query_template.format  # `<@{id}>` by default
    # (query, label, member_only, direct, accept): `accept` lists every raw-markup form
    # a genuine mention of that id can take in a message body, independent of what the
    # search asked for. A usergroup is searched as `<@ID>` (600 literal hits here, all
    # genuine — the documented `<!subteam^…>` markup is unsearchable and degrades to a
    # 1.7M-hit fuzzy match), but a body may legitimately carry EITHER form.
    targets: list[tuple[str, str, bool, bool, tuple[str, ...] | None]] = [
        (q(id=uid), "@you", False, True, (f"<@{uid}",))
    ]
    for gid, handle in await _my_subteams(token, uid):
        targets.append((q(id=gid), f"@{handle}", False, False, (f"<@{gid}", f"<!subteam^{gid}")))
    my_channels: set[str] = set()
    if settings.slack_broadcasts:
        my_channels = await _my_channels(token)
        # QUOTED: a bare <!channel> word-matches the literal word "channel" (460 hits,
        # first page all feed-* spam — the genuine broadcasts never surface, so zero
        # cards despite verification). Quoting makes search match the exact markup
        # (7 hits, all real @channel). Measured 2026-09-02.
        # explicit accept tokens: the query now carries literal quotes, so deriving the
        # verification token from the query would look for `"<!channel>">` and reject
        # every genuine hit.
        targets += [(f'"<!{b}>"', f"@{b}", True, False, (f"<!{b}",)) for b in ("channel", "here", "everyone")]

    count = 0
    for query, label, member_only, direct, accept in targets:
        try:
            count += await _search(token, query, label, seen, member_only, my_channels, direct, accept)
        except Exception as exc:  # noqa: BLE001
            log.warning("[mention %s] %s", label, exc)
    return count


_MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>")
_GROUP_RE = re.compile(r"<!subteam\^[A-Z0-9]+(?:\|(@[^>]+))?>")
_BROADCAST_RE = re.compile(r"<!(channel|here|everyone)>")
_LINK_RE = re.compile(r"<(https?://[^|>]+)(?:\|([^>]*))?>")


async def _format_slack_text(text: str, token: str) -> str:
    """Turn Slack markup into readable text: <@Uxxx> → @Name, <!subteam^…|@grp> → @grp,
    <!here> → @here, <url|label> → label. Without this a thread shows raw <@U06…> ids."""
    if not text:
        return text
    names = {uid: await _user_name(token, uid) for uid in set(_MENTION_RE.findall(text))}
    text = _MENTION_RE.sub(lambda m: f"@{names.get(m.group(1)) or m.group(1)}", text)
    text = _GROUP_RE.sub(lambda m: m.group(1) or "@group", text)
    text = _BROADCAST_RE.sub(lambda m: f"@{m.group(1)}", text)
    text = _LINK_RE.sub(lambda m: m.group(2) or m.group(1), text)
    return text


async def fetch_thread(card) -> list[dict]:
    """Every message in a slack card's thread (conversations.replies), oldest first,
    each tagged with the sender's name + whether it's me — so the whole discussion
    reads in-app instead of a click out to Slack."""
    token = settings.slack_token
    sl = (card.cached or {}).get("slack") or {}
    channel = sl.get("channel")
    if not token or not channel:
        return []
    # thread parent: the permalink's thread_ts if present, else this message's ts
    thread_ts = None
    if card.url:
        thread_ts = parse_qs(urlparse(card.url).query).get("thread_ts", [None])[0]
    thread_ts = thread_ts or sl.get("ts")
    if not thread_ts:
        return []
    try:
        res = await _call(
            "conversations.replies", token,
            {"channel": channel, "ts": thread_ts, "limit": "100"},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[thread %s] %s", channel, exc)
        return []
    uid = (await _me(token))[0]
    out: list[dict] = []
    for m in res.get("messages", []):
        if m.get("subtype") in ("channel_join", "channel_leave"):
            continue
        name = await _user_name(token, m.get("user")) or m.get("username") or "?"
        out.append(
            {
                "user": name,
                "text": await _format_slack_text((m.get("text") or "")[:4000], token),
                "ts": m.get("ts"),
                "is_me": bool(uid) and m.get("user") == uid,
            }
        )
    return out


def _mention_expired(card) -> bool:
    """Has an un-actioned mention outlived CONDUCTOR_SLACK_MENTION_KEEP_DAYS?

    Age comes from the message's OWN slack ts (the same field prune_done uses), not
    from when the card was created — re-carding a months-old thread must not reset
    its clock. Fails SAFE in both directions: 0/negative days disables expiry, and an
    absent or unparseable ts keeps the card. Dropping a genuine ask because of bad
    data is much worse than carrying one card too long."""
    days = settings.slack_mention_keep_days
    if days <= 0:
        return False
    ts = ((card.cached or {}).get("slack") or {}).get("ts")
    try:
        secs = float(ts)
    except (TypeError, ValueError):
        return False
    # float() also accepts "-inf" / "inf" / "nan", which parse but are not timestamps.
    # Only "-inf" actually bites (age becomes +inf, so the card is pruned) — the other
    # two happen to fall the safe way. Make all three safe by construction rather than
    # by accident, since the docstring above promises exactly that.
    if not math.isfinite(secs):
        return False
    return (time.time() - secs) > days * 86400


def _user_held(card_state) -> bool:
    """Has the user explicitly claimed this card, so deleting it would destroy a
    decision they made rather than just drop a stale row?

    Every flag that means "I have handled or deferred this" belongs here, because
    prune goes through delete_card_cascade, which takes the whole LocalState with
    the card — pin, snooze, note, picked option — and no later poll can bring any of
    it back (a card past the keep bound is by definition outside the surfacing
    window, so it never reappears in `seen`).

    - dismissed: handled; the record that the ask is closed.
    - pinned:    an explicit "keep this prominent"; deleting it is the exact opposite.
    - snoozed:   the worst case, because it is silent. api/cards.py hides a snoozed
                 card from the board until the snooze fires, so pruning one mid-snooze
                 makes the ask vanish with nothing on screen to notice. Checked only
                 while the snooze is LIVE — once it fires the card is back on the
                 board and the age bound applies to it again, which is what keeps
                 this from becoming a second unbounded hatch."""
    if not card_state:
        return False
    if card_state.dismissed or card_state.pinned:
        return True
    return bool(card_state.snoozed_until and card_state.snoozed_until > utcnow())


def _keep_slack(card, ls) -> bool:
    """Per-source prune escape hatch (see store.prune_source):
    - a card the user explicitly claimed — dismissed, pinned or snoozed (_user_held)
      — plus a done resolution, never resurfaces just because its message
      transiently dropped out of the search window
    - a @-mention is a one-time ask — keep it until you dismiss/done it, even
      past the 7-day surfacing window (else un-actioned asks like "host a
      postmortem" silently vanish). DMs still prune on absence.

    That mention hatch used to be UNBOUNDED, and nothing else could reach these
    cards: prune_done only ever touches ball == "none", and an unread mention is
    ball == "human" (recompute_ball). So they accumulated forever — measured at 2818
    of 2891 cards on one board, ~96% of the /api/cards payload. The keep window now
    ends at slack_mention_keep_days.

    Bounding it is what made pinned/snoozed load-bearing here: while the hatch was
    unbounded no mention could ever be pruned, so checking `dismissed` alone was
    enough. (PR review)"""
    if _user_held(ls) or ((card.cached or {}).get("slack") or {}).get("done"):
        return True
    if not card.external_id.startswith("mention:"):
        return False  # a DM prunes on absence, as before
    return not _mention_expired(card)


async def _prune_slack(keep: set[str]) -> None:
    await store.prune_source("slack", keep, keep_if=_keep_slack)


async def poll() -> int:
    token = settings.slack_token
    if not token:
        return 0  # disabled until a token is configured
    uid = (await _me(token))[0]
    seen: set[str] = set()
    count = 0
    ok = False
    if settings.slack_dms:
        try:
            count += await _poll_dms(token, uid, seen)
            ok = True
        except Exception as exc:  # noqa: BLE001
            log.warning("[dm] %s", exc)
    if uid:
        try:
            count += await _poll_mentions(token, uid, seen)
            ok = True
        except Exception as exc:  # noqa: BLE001
            log.warning("[mention] %s", exc)
    if ok:  # only prune when at least one fetch succeeded (don't wipe on a fluke)
        await _prune_slack(seen)
    return count
