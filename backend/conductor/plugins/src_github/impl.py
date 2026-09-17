from __future__ import annotations

import asyncio
import datetime
import json
import re
from pathlib import Path

from sqlalchemy import select

from ... import prompts, store
from ...config import settings
from ...db import session_maker
from ...models import Card, CardLink


async def _run_gh(args: list[str], timeout: float = 20) -> str:
    proc = await asyncio.create_subprocess_exec(
        settings.gh_bin,
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise RuntimeError(f"gh {' '.join(args)} timed out after {timeout}s")
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {err.decode()[:300]}")
    return out.decode()


async def fetch_prs() -> list[dict]:
    raw = await _run_gh(
        [
            "search",
            "prs",
            settings.github_query,
            "--json",
            "number,title,url,repository,author,isDraft,state,labels,createdAt,updatedAt",
            "--limit",
            "50",
        ]
    )
    data = json.loads(raw)
    return data if isinstance(data, list) else []


def _repo_name(pr: dict) -> str | None:
    repo = pr.get("repository") or {}
    return repo.get("nameWithOwner") or repo.get("name") if isinstance(repo, dict) else None


async def poll() -> int:
    prs = await fetch_prs()
    count = 0
    for pr in prs:
        repo = _repo_name(pr)
        number = pr.get("number")
        if not repo or number is None:
            continue
        external_id = f"{repo}#{number}"
        author = pr.get("author") or {}
        gh_patch = {
            "number": number,
            "repo": repo,
            "url": pr.get("url"),
            "state": (pr.get("state") or "open").lower(),
            "is_draft": bool(pr.get("isDraft")),
            "author": author.get("login") if isinstance(author, dict) else author,
            "review_requested_me": True,  # query is user-review-requested:@me
            "reviewed_by_me": False,
        }
        detail = await pr_detail(repo, int(number))
        if detail:
            gh_patch["state"] = detail.get("state") or gh_patch["state"]
            gh_patch["ci"] = detail.get("ci")
            gh_patch["review"] = detail.get("review")
            # not `or None`-guarded: None means "I have no review on this PR", a real
            # value that must be able to clear a stale badge
            gh_patch["my_review"] = detail.get("my_review")
            gh_patch["checks"] = detail.get("checks") or []
            gh_patch["reviews"] = detail.get("reviews") or []
            gh_patch["review_requests"] = detail.get("review_requests") or []
        async with session_maker() as session:
            await store.upsert_card(
                session,
                origin="github",
                external_id=external_id,
                title=pr.get("title") or external_id,
                summary=pr.get("title") or "",
                url=pr.get("url"),
                cached_patch={"github": gh_patch},
                link_text=f"{pr.get('title') or ''} {pr.get('url') or ''}",
            )
        count += 1
    return count


def _key_pattern(key: str) -> re.Pattern:
    # exact key, not a substring of a longer key (PROJ-727 must not match PROJ-7271)
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(key)}(?![A-Za-z0-9])", re.IGNORECASE)


async def search_prs_for_key(key: str) -> list[dict]:
    """PRs that belong to a ticket. `gh search` tokenizes (PROJ-727 → ACE + 727 across
    title+body) and matches junk, so: scope to the org (kills global noise), then keep
    only candidates whose TITLE or HEAD BRANCH contains the exact key. Body-only matches
    are dropped — PRs cross-reference other tickets in their body. gh search can't return
    the branch, so each candidate is confirmed via `gh pr view`; its detail is kept
    (embedded as `_detail`) so enrich doesn't have to fetch it again."""
    raw = await _run_gh(
        ["search", "prs", "--owner", settings.github_org, key, "--json",
         "number,title,url,repository", "--limit", "20"]
    )
    candidates = json.loads(raw)
    if not isinstance(candidates, list):
        return []
    pat = _key_pattern(key)
    out: list[dict] = []
    for pr in candidates:
        repo = _repo_name(pr)
        num = pr.get("number")
        if not repo or num is None:
            continue
        detail = await pr_detail(repo, int(num))
        if pat.search(pr.get("title") or "") or pat.search(detail.get("branch") or ""):
            out.append({**pr, "_detail": detail})
    return out


def _check_name(c: dict) -> str:
    return c.get("name") or c.get("context") or c.get("workflowName") or "check"


def _rollup_ts(c: dict) -> str:
    """Sort key for 'which run is current' — prefer completedAt, then startedAt, then
    createdAt (StatusContext). ISO-8601 strings sort chronologically as text."""
    return c.get("completedAt") or c.get("startedAt") or c.get("createdAt") or ""


def _latest_per_check(rollup: list) -> list:
    """Collapse duplicate check names to the LATEST run per name — GitHub's own rollup
    keeps every run, so a re-run leaves the superseded CANCELLED/FAILED entry next to
    the new SUCCESS one. Counting both makes a green PR look failing (PROJ-8175: three
    checks were CANCELLED then re-ran green). Keep only the newest run per name, which
    is what the GitHub UI shows."""
    latest: dict[str, dict] = {}
    for c in rollup:
        name = _check_name(c)
        if name not in latest or _rollup_ts(c) >= _rollup_ts(latest[name]):
            latest[name] = c
    return list(latest.values())


def _ci_summary(rollup: list) -> str:
    if not rollup:
        return "none"
    fail = pending = ok = 0
    for c in rollup:
        concl = (c.get("conclusion") or c.get("state") or "").upper()
        status = (c.get("status") or "").upper()
        if status in ("QUEUED", "IN_PROGRESS", "PENDING", "WAITING") or concl == "PENDING":
            pending += 1
        elif concl in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED",
                       "ACTION_REQUIRED", "STARTUP_FAILURE", "FAILED"):
            fail += 1
        elif concl in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            ok += 1
        else:
            pending += 1
    if fail:
        return "failing"
    if pending:
        return "pending"
    return "passing" if ok else "none"


_REVIEW_STATE = {
    "APPROVED": "approved",
    "CHANGES_REQUESTED": "changes",
    "COMMENTED": "commented",
    "DISMISSED": "dismissed",
    "PENDING": "pending",
}
_CI_FAIL = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE", "FAILED"}
_CI_PENDING = {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING"}


def _ci_checks(rollup: list, limit: int = 15) -> list[dict]:
    """The non-green checks (failing first, then pending) with names + links — the
    'which CI didn't pass' detail. Passing checks are omitted; you only look here when
    something is red or still running."""
    fail, pend = [], []
    for c in rollup:
        name = c.get("name") or c.get("context") or c.get("workflowName") or "check"
        concl = (c.get("conclusion") or c.get("state") or "").upper()
        status = (c.get("status") or "").upper()
        url = c.get("detailsUrl") or c.get("targetUrl")
        if concl in _CI_FAIL:
            fail.append({"name": name, "state": "failing", "url": url})
        elif status in _CI_PENDING or concl == "PENDING":
            pend.append({"name": name, "state": "pending", "url": url})
    return (fail + pend)[:limit]


def _reviews(latest: list) -> list[dict]:
    """Latest review per reviewer → [{user, state}] (approved/changes/commented/…)."""
    out = []
    for r in latest:
        user = (r.get("author") or {}).get("login") or "?"
        raw = (r.get("state") or "").upper()
        out.append({"user": user, "state": _REVIEW_STATE.get(raw, raw.lower())})
    return out


def _review_requests(reqs: list) -> list[str]:
    """Reviewers still requested but who haven't reviewed yet (user login / team slug)."""
    return [(r.get("login") or r.get("slug") or r.get("name") or "?") for r in reqs]


async def pr_detail(repo: str, number: int) -> dict:
    try:
        raw = await _run_gh(
            ["pr", "view", str(number), "-R", repo, "--json",
             "state,statusCheckRollup,reviewDecision,headRefName,title,latestReviews,reviewRequests,author"]
        )
        d = json.loads(raw)
    except Exception:  # noqa: BLE001
        return {}
    rollup = _latest_per_check(d.get("statusCheckRollup") or [])
    latest = d.get("latestReviews") or []
    return {
        "state": (d.get("state") or "").lower(),
        "ci": _ci_summary(rollup),
        "checks": _ci_checks(rollup),
        "review": (d.get("reviewDecision") or "").lower(),
        # MY verdict, derived here rather than in a second gh call: `latestReviews`
        # already carries author + state + body, which is everything _review_status
        # needs. Keeping it out of this dict is what let a stale value survive forever
        # on an ordinary review-requested card — every writer of `my_review` went
        # through brief_prs, which only visits cards the review-requested poll doesn't
        # already cover, so nothing on the poll path could ever repair one. Now the
        # poll maintains it.
        "my_review": _review_status(_my_latest_review(latest, await _my_login())),
        "reviews": _reviews(latest),
        "review_requests": _review_requests(d.get("reviewRequests") or []),
        "branch": d.get("headRefName") or "",
        "title": d.get("title") or "",
        "author": (d.get("author") or {}).get("login") or "",
    }


def _stale(card: Card, now: datetime.datetime, max_age_s: int = 600) -> bool:
    ts = (card.cached or {}).get("pr_search_ts")
    if not ts:
        return True
    try:
        return (now - datetime.datetime.fromisoformat(ts)).total_seconds() > max_age_s
    except (TypeError, ValueError):
        return True


def _pr_status_entry(detail: dict, fallback_state: str = "") -> dict:
    """The cached.prs value for one PR — the live state/CI/reviews the FE reads for the
    PR status chip + hover preview."""
    return {
        "state": detail.get("state") or fallback_state,
        "ci": detail.get("ci") or "none",
        "review": detail.get("review") or "",
        "checks": detail.get("checks") or [],
        "reviews": detail.get("reviews") or [],
        "review_requests": detail.get("review_requests") or [],
        "author": detail.get("author") or "",
    }


async def _add_manual_pr_status(
    card_id: str, pr_status: dict, prev_prs: dict | None = None
) -> dict:
    """Fetch live detail for ALL of a card's PR links into pr_status, skipping refs
    already present. A PR chip should always carry its status hover — whether the link
    was pinned by hand, attached by a plugin source, or auto-discovered from text; a
    chip with status next to one without is an inexplicable distinction. Shared by the
    jira path and the slack/manual/plugin path. (`auto` keeps its OTHER role: marking
    jira search-derived links for the self-heal sweep.)

    ``prev_prs`` (the card's current cached.prs, if the caller has it) is a fallback
    when a ref's fetch fails: `pr_detail` swallows its own errors and returns `{}`
    rather than raising, so a transient gh hiccup must not blank out this ref's
    last-known status — it should just stay stale until the next successful fetch."""
    async with session_maker() as session:
        manual_refs = [
            link.ref
            for link in (
                await session.execute(
                    select(CardLink).where(
                        CardLink.card_id == card_id,
                        CardLink.kind == "pr",
                    )
                )
            ).scalars().all()
            if link.ref
        ]
    for ref in manual_refs:
        if ref in pr_status:
            continue
        repo, sep, num = ref.partition("#")
        if not sep or not num.isdigit():
            continue
        try:
            detail = await pr_detail(repo, int(num))
        except Exception:  # noqa: BLE001
            detail = {}
        if detail:
            pr_status[ref] = _pr_status_entry(detail)
        else:
            stale = (prev_prs or {}).get(ref)
            if stale is not None:
                pr_status[ref] = stale
    return pr_status


async def _enrich_manual_prs(card: Card) -> bool:
    """Enrich a card's PR links into cached.prs so they get a status preview — for
    slack / manual / plugin-source cards (jira rides _enrich_one_jira; a github card IS
    its own PR). Replaces cached.prs with the card's current PR-link set."""
    now = datetime.datetime.now(datetime.timezone.utc)
    pr_status = await _add_manual_pr_status(card.id, {}, prev_prs=(card.cached or {}).get("prs"))
    async with session_maker() as session:
        await store.upsert_card(
            session,
            origin=card.origin,
            external_id=card.external_id,
            cached_patch={"pr_search_ts": now.isoformat()},
            cached_replace={"prs": pr_status},
            create=False,
        )
    return bool(pr_status)


async def enrich_manual_prs(limit: int = 8) -> int:
    """Poll: keep every PR link on non-jira/github cards (slack / manual / plugin
    sources) fresh in cached.prs, throttled per card — a PR chip always carries its
    status hover, however the link got there. jira cards ride enrich_jira_prs."""
    now = datetime.datetime.now(datetime.timezone.utc)
    async with session_maker() as session:
        card_ids = set(
            (
                await session.execute(
                    select(CardLink.card_id).where(CardLink.kind == "pr")
                )
            ).scalars().all()
        )
        cards = (
            (
                await session.execute(
                    select(Card).where(
                        Card.id.in_(card_ids), Card.origin.notin_(["jira", "github"])
                    )
                )
            ).scalars().all()
            if card_ids
            else []
        )
    count = 0
    for card in [c for c in cards if _stale(c, now)][:limit]:
        if await _enrich_manual_prs(card):
            count += 1
    return count


async def _enrich_one_jira(card: Card, now: datetime.datetime) -> bool:
    """Store live state/CI/reviews for a jira ticket's PRs on the card (cached.prs) so the
    FE shows the status chip + hover preview. Covers PRs found by the key-search (exact
    title/branch match, kept as self-healed auto links) AND every other PR link on the
    card the search misses — hand-pinned or text-discovered alike; a chip always carries
    its hover. Returns True if any PR status was stored. Shared by the poll + on-demand
    refresh."""
    j = (card.cached or {}).get("jira") or {}
    keys = [card.external_id] + ([j["parent_key"]] if j.get("parent_key") else [])
    prs = []
    seen_ref: set[str] = set()
    search_errored = False  # a real exception, not just an empty result — see below
    for k in keys:
        try:
            for pr in await search_prs_for_key(k):
                repo = _repo_name(pr)
                num = pr.get("number")
                if not repo or num is None:
                    continue
                ref = f"{repo}#{num}"
                if ref not in seen_ref:
                    seen_ref.add(ref)
                    prs.append(pr)
        except Exception:  # noqa: BLE001
            search_errored = True
            continue
    links: list[dict] = []
    pr_status: dict[str, dict] = {}
    # ALL matched PRs, not a top-N slice: a ticket legitimately spans several repos and
    # retries (PROJ-9406 had 9 across svc-doc-engine(-fe)/gf-id/fms/gf-external-api). A cap
    # here dropped whichever PRs fell past it in gh's opaque search ranking — and since
    # the links are self-healed to the current set every cycle, a link would flap in/out
    # as the ranking shifted (the merged gf-id PR simply vanished). Detail is already
    # fetched in search_prs_for_key, so covering all of them costs no extra gh calls;
    # the search's own --limit 20 (per key) is the natural bound.
    for pr in prs:
        repo = _repo_name(pr)
        num = pr.get("number")
        if not repo or num is None:
            continue
        ref = f"{repo}#{num}"
        detail = pr.get("_detail") or {}  # already fetched in search_prs_for_key
        pr_status[ref] = _pr_status_entry(detail, (pr.get("state") or "").lower())
        links.append({"kind": "pr", "ref": ref, "url": pr.get("url"), "title": ref, "auto": True})
    # self-heal FIRST: drop prior auto pr links (the search re-derives the precise set
    # below) — then enrich, so a swept stale link can't leak its status into cached.prs
    async with session_maker() as session:
        stale_links = (
            await session.execute(
                select(CardLink).where(
                    CardLink.card_id == card.id,
                    CardLink.kind == "pr",
                    CardLink.auto.is_(True),
                )
            )
        ).scalars().all()
        for link in stale_links:
            await session.delete(link)
        await session.commit()
    # also enrich every OTHER PR link the key-search didn't find — hand-attached (PROJ-10778
    # had PROJ-6359's PR pinned to it) or discovered alike; a chip always carries its
    # hover. Display-only: cached.prs never drives a jira card's lane.
    prev_prs = (card.cached or {}).get("prs") or {}
    await _add_manual_pr_status(card.id, pr_status, prev_prs=prev_prs)
    if search_errored:
        # a transient search failure (gh down/rate-limited/timeout) must not wipe
        # previously-known status for refs the (failed) search would have re-derived —
        # fall back to the prior snapshot for anything still missing. A ref a
        # SUCCESSFUL search genuinely no longer finds must still drop: search_errored
        # only flips on a real exception, never on a clean empty result.
        for ref, meta in prev_prs.items():
            pr_status.setdefault(ref, meta)
    async with session_maker() as session:
        await store.upsert_card(
            session,
            origin="jira",
            external_id=card.external_id,
            # `prs` is a full snapshot of the ticket's current PRs → REPLACE it, so the
            # map tracks the visible link set instead of accumulating stale closed PRs.
            cached_patch={"pr_search_ts": now.isoformat()},
            cached_replace={"prs": pr_status},
            extra_links=links or None,
        )
    return bool(pr_status)


async def enrich_jira_prs(limit: int = 8) -> int:
    """Attach the PRs that belong to a jira ticket with live state + CI + reviews.
    Any NON-DONE ticket qualifies — yours to act on, AI-working, or backlog — since
    you want PR status on all live work; done tickets are skipped (their PRs are
    settled). Throttled per-card (~10min) and capped per cycle, so the pool rotates
    through without hammering the gh API."""
    now = datetime.datetime.now(datetime.timezone.utc)
    async with session_maker() as session:
        cards = (
            await session.execute(select(Card).where(Card.origin == "jira"))
        ).scalars().all()

    def _eligible(c) -> bool:
        if ((c.cached or {}).get("jira") or {}).get("is_subtask"):
            return True  # subtasks: PRs live on the parent, surface regardless of status
        return c.ball != "none"  # any live ticket; a done ticket's PRs are settled

    candidates = [c for c in cards if _eligible(c) and _stale(c, now)][:limit]
    count = 0
    for card in candidates:
        if await _enrich_one_jira(card, now):
            count += 1
    return count


async def refresh_card_prs(card_id: str) -> bool:
    """On-demand: refresh ONE card's PR data now, bypassing the poll throttle — used
    when a card detail opens so you see current CI/review immediately instead of
    waiting for the next cycle. jira → re-search its linked PRs; github → refresh the
    PR's own state."""
    now = datetime.datetime.now(datetime.timezone.utc)
    async with session_maker() as session:
        card = await session.get(Card, card_id)
    if not card:
        return False
    if card.origin == "jira":
        await _enrich_one_jira(card, now)
        return True
    if card.origin == "github" and "#" in card.external_id:
        repo, num = card.external_id.rsplit("#", 1)
        try:
            detail = await pr_detail(repo, int(num))
        except (ValueError, TypeError):
            return False
        if not detail:
            return False
        async with session_maker() as session:
            await store.upsert_card(
                session,
                origin="github",
                external_id=card.external_id,
                cached_patch={"github": {
                    "state": detail["state"], "ci": detail["ci"], "review": detail["review"],
                    "my_review": detail.get("my_review"),  # None clears a stale badge
                    "checks": detail.get("checks") or [],
                    "reviews": detail.get("reviews") or [],
                    "review_requests": detail.get("review_requests") or [],
                    "author": detail.get("author") or "",
                }},
                create=False,
            )
        return True
    # slack / manual / other — enrich any hand-linked PRs so they get a status preview too
    return await _enrich_manual_prs(card)


async def refresh_pr_states(limit: int = 20) -> int:
    """Refresh state/CI for github PR cards the review-requested poll() doesn't cover —
    i.e. PR jobs seeded by some other source (a hand-linked PR, a plugin). This is what
    carries a merged PR into Done: gh state merged/closed → recompute_ball → 'none'."""
    async with session_maker() as session:
        cards = (
            await session.execute(select(Card).where(Card.origin == "github"))
        ).scalars().all()
    # poll() already refreshes review-requested cards each cycle; only handle the rest.
    todo = [
        c for c in cards
        if not ((c.cached or {}).get("github") or {}).get("review_requested_me")
    ]
    count = 0
    for card in todo[:limit]:
        gh = (card.cached or {}).get("github") or {}
        repo, num = gh.get("repo"), gh.get("number")
        if not repo or num is None:
            repo, _, n = card.external_id.rpartition("#")
            num = int(n) if n.isdigit() else None
        if not repo or num is None:
            continue
        detail = await pr_detail(repo, int(num))
        if not detail:
            continue
        async with session_maker() as session:
            await store.upsert_card(
                session,
                origin="github",
                external_id=card.external_id,
                cached_patch={"github": {
                    "state": detail["state"], "ci": detail["ci"], "review": detail["review"],
                    "my_review": detail.get("my_review"),  # None clears a stale badge
                    "checks": detail.get("checks") or [],
                    "reviews": detail.get("reviews") or [],
                    "review_requests": detail.get("review_requests") or [],
                    "author": detail.get("author") or "",
                }},
                create=False,
            )
        count += 1
    return count


# --- claude PR brief + "my review" status ----------------------------------------
_brief_sem = asyncio.Semaphore(3)  # bound concurrent gh-fetch + claude to keep load sane
_briefing: set[str] = set()
_my_login_cache: str | None = None
# A review-recommendation tool records its suggestion in the review BODY
# ("🤖 **Suggested verdict: X**"). It posts REQUEST_CHANGES as a real
# CHANGES_REQUESTED review, but "approve" only as a COMMENTED review — it never
# formally approves — so a suggested-approve must not read as a real Approved. The
# GitHub review *state* is the source of truth for what I formally did; the body only
# tells suggested-approve apart from a plain comment.
_VERDICT_RE = re.compile(r"Suggested verdict:\s*\**\s*(APPROVE|REQUEST_CHANGES|COMMENT)", re.I)


async def _my_login() -> str | None:
    """My github login. A review-recommendation tool can post under my own account, so
    a review by this login is *my* verdict — distinct from other bots / copilot /
    coderabbit reviewing the same PR."""
    global _my_login_cache
    if _my_login_cache is None:
        if settings.github_login:
            _my_login_cache = settings.github_login
        else:
            try:
                _my_login_cache = (await _run_gh(["api", "user", "--jq", ".login"], timeout=15)).strip()
            except Exception:  # noqa: BLE001
                _my_login_cache = ""
    return _my_login_cache or None


async def _pr_body_and_reviews(repo: str, number: int) -> dict | None:
    try:
        raw = await _run_gh(
            ["pr", "view", str(number), "-R", repo, "--json", "title,body,reviews"], timeout=25
        )
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def _my_latest_review(reviews: list[dict], my_login: str | None) -> dict | None:
    """My most recent (non-pending) review on the PR."""
    if not my_login:
        return None
    mine = [
        r for r in reviews
        if (r.get("author") or {}).get("login") == my_login and (r.get("state") or "") != "PENDING"
    ]
    return max(mine, key=lambda r: r.get("submittedAt") or "") if mine else None


def _review_status(review: dict | None) -> str | None:
    """My review status, honest about formal vs suggested:
      APPROVED         — I formally clicked Approve (real approval)
      CHANGES_REQUESTED— I/an automated tool formally requested changes (blocking)
      SUGGEST_APPROVE  — an automated tool only commented but recommends approve
                         (NOT approved)
      COMMENTED        — a plain comment / no verdict
    """
    if not review:
        return None
    state = (review.get("state") or "").upper()
    if state == "APPROVED":
        return "APPROVED"
    if state == "CHANGES_REQUESTED":
        return "CHANGES_REQUESTED"
    # COMMENTED / DISMISSED: distinguish a "suggest approve" from a plain comment
    m = _VERDICT_RE.search(review.get("body") or "")
    if m and m.group(1).upper() == "APPROVE":
        return "SUGGEST_APPROVE"
    return "COMMENTED"


async def refresh_pr_card(origin: str, external_id: str, repo: str, number: int) -> None:
    """Re-derive a PR card's live state + my-review status NOW and write them back.

    Conductor renders the board from ``cached``, not from GitHub, so an action that
    changes the PR without touching the card produces no visible change — and the
    frontend refetches right afterwards, which makes it look like the UI checked and
    found nothing rather than like it never looked.

    Nothing else covers this QUICKLY. The poll paths do maintain ``my_review`` (it
    rides ``pr_detail``), but on their own cadence — so without this an approve shows
    up whenever the next cycle lands rather than when you pressed the button. For merge
    the same delay leaves ``state`` at "open", which is what keeps a merged PR sitting
    in Need Human instead of falling to Done.

    Same shape as ``jira.set_hold``: do the thing, reflect it immediately with a narrow
    patch, let the next poll re-confirm. Best-effort by construction — the action has
    already landed on GitHub, so a failure here costs freshness, not correctness. If
    GitHub happens to answer from a replica that has not caught up, this writes the
    pre-action value and the poll corrects it, which is exactly today's behavior.
    """
    detail = await pr_detail(repo, number)
    if not detail:
        return
    # the same keys poll()/refresh_pr_states write, so a card that just merged reaches
    # ball == "none" without waiting for the dates loop
    patch: dict = {
        key: detail[key]
        for key in ("state", "ci", "review", "checks", "reviews", "review_requests")
        if detail.get(key) is not None
    }
    # my_review is set unconditionally, unlike the block above: None is a real value
    # ("I have no review on this PR") and must be able to clear a stale badge
    patch["my_review"] = detail.get("my_review")
    async with session_maker() as session:
        await store.upsert_card(
            session, origin=origin, external_id=external_id,
            cached_patch={"github": patch}, create=False,
        )


async def _brief_pr(title: str, body: str, review: dict | None) -> str | None:
    review_txt = (review.get("body") or "") if review else ""
    # template from prompts.py (user-overridable) + the non-overridable no-tools guard
    prompt = (
        prompts.prompt(
            "pr_brief", title=title, body=body[:2000], review=review_txt[:2000] or "(none)"
        )
        + prompts.NO_TOOLS_GUARD
    )
    proc = await asyncio.create_subprocess_exec(
        # --strict-mcp-config: skip every MCP server; a summary needs no tools and
        # MCP startup dominated the latency AND token bill of each brief.
        # --disallowedTools: block the built-ins too (see slack._brief for the incident)
        settings.claude_bin, "-p", prompt,
        "--model", settings.brief_model, "--strict-mcp-config",
        "--disallowedTools", "*",  # deny-all: a name list rots as the CLI grows tools
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        cwd=str(Path.home()),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        return None
    if proc.returncode != 0:
        return None
    return out.decode().strip()[:600] or None


async def _maybe_brief_pr(card: Card) -> bool:
    """Brief a PR card: what the PR does + my latest review verdict on it. Keyed by
    the latest review's submittedAt, so a new verdict re-briefs but an unchanged one is
    skipped. Returns True only when it actually (re)briefed."""
    eid = card.external_id
    if eid in _briefing:
        return False
    gh = (card.cached or {}).get("github") or {}
    repo, num = gh.get("repo"), gh.get("number")
    if not repo or num is None:
        repo, _, n = eid.rpartition("#")
        num = int(n) if n.isdigit() else None
    if not repo or num is None:
        return False
    async with _brief_sem:
        data = await _pr_body_and_reviews(repo, int(num))
        if data is None:
            return False
        review = _my_latest_review(data.get("reviews") or [], await _my_login())
        key = (review or {}).get("submittedAt") or "none"
        status = _review_status(review)
        # keep the "my review" status current every cycle (cheap, no claude) so the
        # badge tracks my latest review even when the brief text is unchanged.
        if gh.get("my_review") != status:
            async with session_maker() as session:
                await store.upsert_card(
                    session, origin="github", external_id=eid,
                    cached_patch={"github": {"my_review": status}}, create=False,
                )
        if gh.get("brief") and gh.get("brief_key") == key:
            return False  # brief already current for this review
        _briefing.add(eid)
        try:
            brief = await _brief_pr(data.get("title") or eid, data.get("body") or "", review)
            if not brief:
                return False
            async with session_maker() as session:
                await store.upsert_card(
                    session, origin="github", external_id=eid,
                    cached_patch={"github": {"brief": brief, "brief_key": key}},
                    create=False,
                )
            return True
        finally:
            _briefing.discard(eid)


async def brief_prs() -> int:
    """Generate/refresh briefs for PR cards the review-requested poll() doesn't
    already cover (same split as refresh_pr_states) — those cards get their context
    from poll() itself, so briefing them again would be redundant. Concurrency is
    bounded by _brief_sem; unchanged reviews skip the claude call, so steady state is
    cheap."""
    async with session_maker() as session:
        cards = (
            await session.execute(select(Card).where(Card.origin == "github"))
        ).scalars().all()
    todo = [
        c for c in cards
        if not ((c.cached or {}).get("github") or {}).get("review_requested_me")
    ]
    if not todo:
        return 0
    results = await asyncio.gather(*(_maybe_brief_pr(c) for c in todo), return_exceptions=True)
    return sum(1 for r in results if r is True)
