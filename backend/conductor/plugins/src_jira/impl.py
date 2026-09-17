from __future__ import annotations

import asyncio
import datetime
import json
import re
import textwrap

from sqlalchemy import select

from ... import store
from ...config import settings
from ...db import session_maker
from ...models import Card, CardLink, Pin


async def _run_acli(args: list[str], timeout: float = 30) -> str:
    proc = await asyncio.create_subprocess_exec(
        settings.acli_bin,
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
        raise RuntimeError(f"acli {' '.join(args)} timed out after {timeout}s")
    if proc.returncode != 0:
        raise RuntimeError(f"acli {' '.join(args)} failed: {err.decode()[:300]}")
    return out.decode()


def _extract_items(data) -> list[dict]:
    """acli --json may return a bare list or a dict wrapper; handle both."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("workItems", "workitems", "issues", "items", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _norm_issue(it: dict) -> dict | None:
    fields = it.get("fields") if isinstance(it.get("fields"), dict) else it
    key = it.get("key") or fields.get("key")
    if not key:
        return None
    summary = fields.get("summary") or it.get("summary") or ""

    status = fields.get("status") or it.get("status")
    status_name = status.get("name") if isinstance(status, dict) else status
    status_category = ""
    if isinstance(status, dict):
        sc = status.get("statusCategory") or {}
        status_category = sc.get("key") or sc.get("name") or ""

    labels = fields.get("labels") or it.get("labels") or []
    if not isinstance(labels, list):
        labels = []

    priority = fields.get("priority") or it.get("priority")
    priority_name = priority.get("name") if isinstance(priority, dict) else priority

    issuetype = fields.get("issuetype")
    it_name = issuetype.get("name") if isinstance(issuetype, dict) else issuetype
    is_subtask = (
        bool(issuetype.get("subtask")) if isinstance(issuetype, dict) else it_name == "Sub-task"
    )
    parent = fields.get("parent")
    parent_key = parent_summary = None
    if isinstance(parent, dict):
        parent_key = parent.get("key")
        parent_summary = (parent.get("fields") or {}).get("summary")

    return {
        "key": key,
        "summary": summary,
        "status": status_name,
        "status_category": status_category,
        "labels": labels,
        "priority": priority_name,
        "updated": fields.get("updated") or it.get("updated"),
        "issuetype": it_name,
        "is_subtask": is_subtask,
        "parent_key": parent_key,
        "parent_summary": parent_summary,
    }


async def _search(jql: str) -> list[dict]:
    raw = await _run_acli(
        ["jira", "workitem", "search", "--jql", jql,
         "--fields", "key,summary,status,assignee,labels,priority,issuetype",
         "--limit", "200", "--json"]
    )
    return _extract_items(json.loads(raw))


async def fetch_parent(key: str) -> tuple[str | None, str | None]:
    """`search` doesn't allow the `parent` field, so fetch it via `view` (which
    does) — only for subtasks, only when not already known."""
    raw = await _run_acli(["jira", "workitem", "view", key, "--fields", "parent", "--json"])
    p = (json.loads(raw).get("fields") or {}).get("parent")
    if isinstance(p, dict):
        return p.get("key"), (p.get("fields") or {}).get("summary")
    return None, None


_parent_cache: dict[str, tuple[str | None, str | None]] = {}


async def _upsert_issue(norm: dict, *, pinned: bool) -> None:
    # subtask parent isn't returned by search; fetch it via `view` ONCE per subtask
    # and cache it — re-fetching every poll made the whole cycle take minutes.
    if norm.get("is_subtask") and not norm.get("parent_key"):
        key = norm["key"]
        if key not in _parent_cache:
            try:
                _parent_cache[key] = await fetch_parent(key)
            except Exception:  # noqa: BLE001
                _parent_cache[key] = (None, None)
        norm["parent_key"], norm["parent_summary"] = _parent_cache[key]
    jira_patch = {
        "status": norm["status"],
        "status_category": norm["status_category"],
        "labels": norm["labels"],
        "priority": norm["priority"],
        "assignee_me": True,  # board filter / pins are mine to act on
        "pinned": pinned,
        "issuetype": norm.get("issuetype"),
        "is_subtask": norm.get("is_subtask"),
        "parent_key": norm.get("parent_key"),
        "parent_summary": norm.get("parent_summary"),
    }
    browse_url = f"{settings.jira_base_url}/browse/{norm['key']}"
    links = [
        {"kind": "jira", "ref": norm["key"], "url": browse_url, "title": norm["key"], "auto": True}
    ]
    if norm.get("parent_key"):
        pk = norm["parent_key"]
        links.append(
            {"kind": "jira", "ref": pk, "url": f"{settings.jira_base_url}/browse/{pk}",
             "title": f"↑ {pk}", "auto": True}
        )
    async with session_maker() as session:
        await store.upsert_card(
            session,
            origin="jira",
            external_id=norm["key"],
            title=norm["summary"],
            summary=norm["summary"],
            url=browse_url,
            cached_patch={"jira": jira_patch},
            link_text=norm["summary"],
            extra_links=links,
        )


async def _pinned_keys() -> list[str]:
    async with session_maker() as session:
        return [r.key for r in (await session.execute(select(Pin))).scalars().all()]


async def set_hold(key: str, hold: bool, *, registry: list[dict] | None = None) -> None:
    """Add or remove the hold label on a Jira ticket, then reflect it on the
    card right away (recompute_ball → Need Human / back) with a labels-only patch — a
    full re-pull would clobber the pinned flag. The next poll re-confirms from Jira.

    ``registry``: the caller's stage snapshot, forwarded to the upsert below. This runs
    in its OWN session, so without it the caller cannot share a lane set with the
    lane_change this fires — and a hold flip is exactly what moves the ball to or from
    ``human``, so both sides really do derive a lane."""
    label = settings.jira_hold_label
    flag = "--labels" if hold else "--remove-labels"
    await _run_acli(["jira", "workitem", "edit", "--key", key, flag, label])
    async with session_maker() as session:
        card = (
            await session.execute(
                select(Card).where(Card.origin == "jira", Card.external_id == key)
            )
        ).scalar_one_or_none()
        prev = list(((card.cached or {}).get("jira") or {}).get("labels") or []) if card else []
        labels = [x for x in prev if x != label] + ([label] if hold else [])
        await store.upsert_card(
            session, origin="jira", external_id=key,
            cached_patch={"jira": {"labels": labels}}, registry=registry,
        )


async def fetch_keys(keys: list[str]) -> int:
    """Fetch + upsert specific ticket keys (pins, or an immediate add)."""
    if not keys:
        return 0
    jql = "key in (" + ",".join(keys) + ")"
    count = 0
    for it in await _search(jql):
        norm = _norm_issue(it)
        if norm:
            await _upsert_issue(norm, pinned=True)
            count += 1
    return count


# In ADF, formatting lives in a node's MARKS — these characters inside its text are
# ordinary content ("**kwargs", "*args", "a_b_c"), so they must survive as themselves
# instead of being read back as emphasis by the renderer.
_MD_SPECIAL = re.compile(r"([\\`*_~\[\]])")
# A line that merely STARTS like a block marker ("- 5% off", "1. Foo", "# 3 open") would
# be re-parsed as a list/heading downstream — escape the marker instead. MULTILINE
# because a hardBreak puts later lines inside the same paragraph.
_LEADING_MARKER = re.compile(r"^(\s*)(?:([-*+>]|#{1,6})|(\d+)([.)]))(\s)", re.M)
# …and a line that IS a rule ("---") would become an <hr>. Only `-` can reach here:
# `*` and `_` are already escaped by _MD_SPECIAL.
_RULE_LINE = re.compile(r"^(\s*)(-(?:\s*-){2,}\s*)$", re.M)


def _escape_marker(m: re.Match) -> str:
    """Backslash the character that makes it a marker. For an ordered marker that's
    the dot — `\\1.` would print literally, since a digit isn't escapable."""
    if m.group(3):
        return f"{m.group(1)}{m.group(3)}\\{m.group(4)}{m.group(5)}"
    return f"{m.group(1)}\\{m.group(2)}{m.group(5)}"


def _escape_markdown(text: str) -> str:
    """Make raw text safe to hand over AS markdown: every character a renderer would act
    on stays visible instead. For text that never passed through the ADF converter this
    is the whole of the escaping it would otherwise have received."""
    out = _MD_SPECIAL.sub(r"\\\1", text)
    out = _LEADING_MARKER.sub(_escape_marker, out)
    return _RULE_LINE.sub(r"\1\\\2", out)


def _adf_edges(text: str) -> tuple[str, str, str]:
    """(leading ws, trimmed body, trailing ws) — emphasis delimiters must hug their
    text or markdown won't open the span (`** bold **` renders literally)."""
    return text[: len(text) - len(text.lstrip())], text.strip(), text[len(text.rstrip()):]


def _adf_marks(text: str, marks: list) -> str:
    """Wrap one text node in its ADF marks. `code` wins outright: a markdown code
    span is literal, so no emphasis can live inside it — and nothing inside needs
    escaping either."""
    kinds = {m.get("type") for m in marks if isinstance(m, dict)}
    if "code" in kinds and text.strip():
        fence = "`" * (max((len(r) for r in re.findall("`+", text)), default=0) + 1)
        pad = " " if text.startswith("`") or text.endswith("`") else ""
        text = f"{fence}{pad}{text}{pad}{fence}"
    else:
        text = _MD_SPECIAL.sub(r"\\\1", text)
        if text.strip():
            lead, body, tail = _adf_edges(text)
            if "strike" in kinds:
                body = f"~~{body}~~"
            if {"em", "strong"} <= kinds:
                # NOT ***text***: a renderer scanning ** first reads that as bold
                # wrapping stray literal asterisks
                body = f"**_{body}_**"
            elif "em" in kinds:
                body = f"*{body}*"
            elif "strong" in kinds:
                body = f"**{body}**"
            text = f"{lead}{body}{tail}"
    for m in marks:
        href = (m.get("attrs") or {}).get("href") if isinstance(m, dict) else None
        if isinstance(m, dict) and m.get("type") == "link" and href:
            text = f"[{text}]({href})"
    return text


def _adf_inline(node) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    t, attrs = node.get("type"), (node.get("attrs") or {})
    if t == "text":
        return _adf_marks(node.get("text", ""), node.get("marks") or [])
    if t == "hardBreak":
        return "  \n"  # two trailing spaces = a markdown line break
    if t == "mention":
        return attrs.get("text") or "@mention"
    if t == "emoji":
        return attrs.get("text") or attrs.get("shortName") or ""
    if t == "status":
        return f"`{(attrs.get('text') or '').strip()}`"
    if t == "date":
        return attrs.get("timestamp") or ""
    if t in ("inlineCard", "blockCard", "embedCard"):
        data = attrs.get("data") if isinstance(attrs.get("data"), dict) else {}
        return attrs.get("url") or data.get("url") or ""
    if t == "media":
        return f"(attachment: {attrs.get('alt') or attrs.get('id') or 'file'})"
    return _adf_inlines(node.get("content") or [])


def _adf_inlines(nodes: list) -> str:
    return "".join(_adf_inline(n) for n in nodes)


def _adf_raw(nodes: list) -> str:
    """Text with every mark ignored — a code block's content is already literal."""
    out = []
    for n in nodes:
        if not isinstance(n, dict):
            continue
        t = n.get("type")
        out.append(
            n.get("text", "") if t == "text"
            else "\n" if t == "hardBreak"
            else _adf_raw(n.get("content") or [])
        )
    return "".join(out)


def _adf_list(kind: str, attrs: dict, items: list) -> str:
    start = int(attrs.get("order") or 1) if kind == "orderedList" else 1
    out = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        marker = f"{start + i}. " if kind == "orderedList" else "- "
        # continuation lines sit under the marker, so a nested list stays nested
        block = textwrap.indent(_adf_body(item.get("content") or []), " " * len(marker))
        out.append(marker + (block[len(marker):] if block[: len(marker)].isspace() else block))
    return "\n".join(out)


def _adf_cell(node) -> str:
    """One line per cell — a markdown table row can't carry block structure."""
    return " ".join(_adf_body(node.get("content") or []).replace("|", "\\|").split())


def _adf_table(rows: list) -> str:
    """ADF marks header cells individually (`tableHeader`), markdown only has a header
    ROW — so row zero is the header only when ADF says so. Promoting it regardless
    turned the first data row of a headerless table into one."""
    grid = [
        [
            (c.get("type") == "tableHeader", _adf_cell(c))
            for c in (r.get("content") or [])
            if isinstance(c, dict) and c.get("type") in ("tableCell", "tableHeader")
        ]
        for r in rows
        if isinstance(r, dict) and r.get("type") == "tableRow"
    ]
    if not grid:
        return ""
    width = max(len(r) for r in grid)
    headed = any(is_header for is_header, _ in grid[0])

    def row(cells: list[tuple[bool, str]], *, bold_headers: bool) -> str:
        # a header cell outside row zero is a ROW header, which markdown can't express —
        # bold keeps the distinction Jira draws instead of erasing it
        out = [f"**{text}**" if bold_headers and is_header and text else text for is_header, text in cells]
        return "| " + " | ".join(out + [""] * (width - len(out))) + " |"

    rule = "| " + " | ".join(["---"] * width) + " |"
    # a headerless table still needs the separator (it is what makes it a table), so it
    # gets an EMPTY header row and the renderer drops the empty <thead>
    lines = [row(grid[0], bold_headers=False), rule] if headed else ["| " + " | ".join([""] * width) + " |", rule]
    lines += [row(cells, bold_headers=True) for cells in (grid[1:] if headed else grid)]
    return "\n".join(lines)


def _adf_block(node) -> str:
    if not isinstance(node, dict):
        return ""
    t, attrs, kids = node.get("type"), (node.get("attrs") or {}), (node.get("content") or [])
    if t == "heading":
        try:
            level = min(max(int(attrs.get("level") or 3), 1), 6)
        except (TypeError, ValueError):
            level = 3
        return f"{'#' * level} {_adf_inlines(kids).strip()}"
    if t == "paragraph":
        text = _LEADING_MARKER.sub(_escape_marker, _adf_inlines(kids).rstrip())
        return _RULE_LINE.sub(r"\1\\\2", text)
    if t == "codeBlock":
        body = _adf_raw(kids).rstrip()
        # a fence must outrun the longest backtick run INSIDE it, or the block ends
        # early and the rest of the description re-parses as prose
        fence = "`" * max(3, max((len(r) for r in re.findall("`+", body)), default=0) + 1)
        lang = re.sub(r"[^\w+#.-]", "", str(attrs.get("language") or ""))
        return f"{fence}{lang}\n{body}\n{fence}"
    if t == "rule":
        return "---"
    if t in ("bulletList", "orderedList"):
        return _adf_list(t, attrs, kids)
    if t in ("blockquote", "panel"):
        body = _adf_body(kids)
        if attrs.get("panelType"):
            body = f"**{attrs['panelType']}**\n\n{body}"
        # prefix blank lines too, so a multi-paragraph quote stays ONE quote
        return textwrap.indent(body, "> ", lambda _line: True)
    if t in ("expand", "nestedExpand"):
        return f"**{attrs.get('title') or 'details'}**\n\n{_adf_body(kids)}"
    if t in ("blockCard", "embedCard"):
        return _adf_inline(node)  # its URL lives in attrs; recursing finds no content
    if t == "table":
        return _adf_table(kids)
    if t in ("taskList", "decisionList"):
        # a glyph, not GFM `- [x]`: the renderer has no task-list branch, so the
        # brackets would show up literally
        return "\n".join(
            f"- {'☑' if (i.get('attrs') or {}).get('state') in ('DONE', 'DECIDED') else '☐'} "
            + _adf_inlines(i.get("content") or [])
            for i in kids
            if isinstance(i, dict)
        )
    if t in ("mediaSingle", "mediaGroup"):
        return _adf_inlines(kids)
    return _adf_body(kids)  # doc / listItem / anything unknown: recurse


def _adf_body(nodes: list) -> str:
    return "\n\n".join(b for b in (_adf_block(n) for n in nodes) if b.strip())


def _adf_to_markdown(node) -> str:
    """Atlassian Document Format → Markdown. The old flattener dropped every bit of
    structure it walked past — headings became bare lines, list markers vanished,
    code lost its backticks — leaving the UI nothing to render but a wall of text."""
    return _adf_body(node.get("content") or []) if isinstance(node, dict) else ""


async def fetch_description(key: str) -> str:
    raw = await _run_acli(["jira", "workitem", "view", key, "--json"])
    d = json.loads(raw)
    fields = d.get("fields") if isinstance(d.get("fields"), dict) else d
    desc = fields.get("description")
    if isinstance(desc, str):
        # A legacy (non-ADF) description is Jira wiki markup, not markdown — and it
        # never met the converter's escaping. Neutralise it so the body we hand over
        # as markdown still reads as the characters the reporter typed.
        return _escape_markdown(desc.strip())
    return _adf_to_markdown(desc).strip() if desc else ""


async def fetch_comments(key: str) -> list[dict]:
    """A ticket's comments as {body(markdown), id, created}, oldest→newest.

    Deliberately `workitem view --fields comment` and NOT `comment list`: the list
    endpoint hands back an acli-flattened plain-text body — every heading / bold /
    code / link collapsed to bare double-spaces, all newlines gone. `view` returns
    the real ADF, which we run through the same _adf_to_markdown fetch_description
    uses, so a caller (see actions/resume.py) can render it as markdown."""
    raw = await _run_acli(["jira", "workitem", "view", key, "--fields", "comment", "--json"])
    d = json.loads(raw)
    fields = d.get("fields") if isinstance(d.get("fields"), dict) else d
    comments = ((fields or {}).get("comment") or {}).get("comments") or []
    out: list[dict] = []
    for c in comments:
        body = c.get("body")
        if isinstance(body, dict):
            md = _adf_to_markdown(body).strip()
        else:
            # a legacy (non-ADF) string body is Jira wiki markup, not markdown —
            # neutralise it so it renders as typed, mirroring fetch_description
            md = _escape_markdown(body.strip()) if body else ""
        out.append({"body": md, "id": c.get("id"), "created": c.get("created") or ""})
    out.sort(key=_created_key)  # ascending by instant — callers take the last match
    return out


_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def _created_key(c: dict) -> datetime.datetime:
    """Sort comments by the true instant, not the raw string. Jira `created` is
    ISO-8601 with an offset (…+0800); a lexicographic sort only matches chronology
    while every offset is identical, which breaks across a DST boundary in one
    response. Unparseable/absent → oldest."""
    try:
        return datetime.datetime.fromisoformat(c["created"])
    except (ValueError, TypeError):
        return _EPOCH


async def fetch_brief(key: str) -> dict:
    """Title / status / assignee for ONE ticket — the jira link chip's hover preview.
    `search` is board-JQL-scoped, so a linked ticket outside the board needs a `view`."""
    raw = await _run_acli(
        ["jira", "workitem", "view", key, "--fields", "summary,status,assignee", "--json"],
        timeout=20,
    )
    f = json.loads(raw).get("fields") or {}
    status = f.get("status")
    assignee = f.get("assignee")
    return {
        "title": f.get("summary"),
        "status": status.get("name") if isinstance(status, dict) else status,
        "assignee": assignee.get("displayName") if isinstance(assignee, dict) else assignee,
    }


def _linked_jira_stale(card: Card, now: datetime.datetime, max_age_s: int = 600) -> bool:
    ts = (card.cached or {}).get("jira_link_ts")
    if not ts:
        return True
    try:
        return (now - datetime.datetime.fromisoformat(ts)).total_seconds() > max_age_s
    except (TypeError, ValueError):
        return True


async def _enrich_card_jiras(card: Card, now: datetime.datetime) -> bool:
    """Hover data for a card's LINKED jira tickets into ``cached.jiras`` — every jira
    chip gets the same hover, however the link was attached (hand-pinned, discovered
    from text, or a plugin source's). The card's OWN ticket (a jira card's self-link)
    is skipped: its data already rides ``cached.jira``.

    Also mirrors the linked ticket's PRs (its jira-origin card's ``cached.prs``, if any)
    onto ``out[ref]["prs"]`` so a card that only LINKS to a ticket (officraft/slack/manual
    origin) shows that ticket's PRs too — no extra GitHub calls, just a copy of what
    ``enrich_jira_prs`` already computed on the jira card. Absent/empty → key omitted."""
    async with session_maker() as session:
        refs = [
            link.ref
            for link in (
                await session.execute(
                    select(CardLink).where(
                        CardLink.card_id == card.id, CardLink.kind == "jira"
                    )
                )
            ).scalars().all()
            if link.ref and not (card.origin == "jira" and link.ref == card.external_id)
        ]
    out: dict = {}
    for ref in refs:
        try:
            out[ref] = await fetch_brief(ref)
        except Exception:  # noqa: BLE001 — an unreadable ticket just shows a plain chip
            continue
    if out:
        # single batched lookup (SAME session) for every linked ticket's jira-origin
        # card, rather than one query per ref.
        async with session_maker() as session:
            jira_cards = (
                await session.execute(
                    select(Card).where(
                        Card.origin == "jira", Card.external_id.in_(list(out.keys()))
                    )
                )
            ).scalars().all()
        for jc in jira_cards:
            prs = (jc.cached or {}).get("prs")
            if prs:
                out[jc.external_id]["prs"] = prs
    async with session_maker() as session:
        await store.upsert_card(
            session,
            origin=card.origin,
            external_id=card.external_id,
            cached_patch={"jira_link_ts": now.isoformat()},
            # snapshot semantics: track the current link set, don't accumulate stale keys
            cached_replace={"jiras": out},
            create=False,
        )
    return bool(out)


async def enrich_linked_jiras(limit: int = 8) -> int:
    """Poll: keep linked-jira hover data fresh on every card that carries a jira link
    (a PR job's ticket link, a slack mention's ticket, a plugin card's ref), throttled
    per card like the PR enrichment."""
    now = datetime.datetime.now(datetime.timezone.utc)
    async with session_maker() as session:
        card_ids = set(
            (
                await session.execute(select(CardLink.card_id).where(CardLink.kind == "jira"))
            ).scalars().all()
        )
        cards = (
            (await session.execute(select(Card).where(Card.id.in_(card_ids)))).scalars().all()
            if card_ids
            else []
        )
    count = 0
    for card in [c for c in cards if _linked_jira_stale(c, now)][:limit]:
        if await _enrich_card_jiras(card, now):
            count += 1
    return count


async def refresh_card_jiras(card_id: str) -> bool:
    """On-demand (drawer open): refresh ONE card's linked-jira hover data now."""
    async with session_maker() as session:
        card = await session.get(Card, card_id)
    if not card:
        return False
    return await _enrich_card_jiras(card, datetime.datetime.now(datetime.timezone.utc))


async def transition_issue(key: str, status: str) -> None:
    """Move a ticket to another status (e.g. Building → Hardening). acli validates the
    transition server-side; an illegal move raises with jira's own reason, which the
    endpoint surfaces verbatim."""
    await _run_acli(
        ["jira", "workitem", "transition", "--key", key, "--status", status, "--yes", "--json"],
        timeout=30,
    )


async def board_statuses() -> list[str]:
    """The status vocabulary of the user's ACTUAL workflow — distinct statuses across
    the board's jira cards. acli can't list a ticket's legal transitions, so the hover
    offers these and lets the transition call validate server-side."""
    async with session_maker() as session:
        cards = (
            await session.execute(select(Card).where(Card.origin == "jira"))
        ).scalars().all()
    seen = {((c.cached or {}).get("jira") or {}).get("status") for c in cards}
    return sorted(s for s in seen if s)


async def refresh_linking_cards(key: str) -> int:
    """After a transition: refresh hover data on every card that LINKS this ticket, so
    open drawers/boards correct within a WS tick instead of the next throttle window.
    This runs inside the transition request/response (api/jira.py), so a widely-linked
    ticket must not serialize N acli calls — concurrency-bounded like poll_dates."""
    now = datetime.datetime.now(datetime.timezone.utc)
    async with session_maker() as session:
        card_ids = set(
            (
                await session.execute(
                    select(CardLink.card_id).where(
                        CardLink.kind == "jira", CardLink.ref == key
                    )
                )
            ).scalars().all()
        )
        cards = (
            (await session.execute(select(Card).where(Card.id.in_(card_ids)))).scalars().all()
            if card_ids
            else []
        )
    sem = asyncio.Semaphore(5)

    async def one(card: Card) -> bool:
        async with sem:
            return await _enrich_card_jiras(card, now)

    results = await asyncio.gather(*(one(card) for card in cards))
    return sum(results)


async def fetch_dates(key: str) -> dict:
    """Due date (+ Target Release Date if configured). `search` can't return
    these fields, so fetch via `view` per ticket."""
    fields = "duedate"
    if settings.jira_trd_field:
        fields += f",{settings.jira_trd_field}"
    raw = await _run_acli(
        ["jira", "workitem", "view", key, "--fields", fields, "--json"], timeout=20
    )
    f = json.loads(raw).get("fields") or {}
    out: dict = {"due_date": f.get("duedate")}
    if settings.jira_trd_field:
        trd = f.get(settings.jira_trd_field)
        # custom date fields come back as a plain string or {"value": ...}
        out["target_release_date"] = (
            trd if isinstance(trd, str) else trd.get("value") if isinstance(trd, dict) else None
        )
    return out


async def poll_dates() -> int:
    """Refresh due dates for all jira cards in its own slow loop. acli view is
    per-ticket and flaky, so cap concurrency and treat any failure as
    'keep the prior value'."""
    async with session_maker() as session:
        keys = [
            c.external_id
            for c in (
                await session.execute(select(Card).where(Card.origin == "jira"))
            ).scalars().all()
        ]
    if not keys:
        return 0
    sem = asyncio.Semaphore(5)

    async def one(key: str) -> None:
        async with sem:
            try:
                dates = await fetch_dates(key)
            except Exception:  # noqa: BLE001
                return  # acli hiccup — leave the prior value in place
            async with session_maker() as session:
                await store.upsert_card(
                    session,
                    origin="jira",
                    external_id=key,
                    cached_patch={"jira": dates},
                    create=False,
                )

    await asyncio.gather(*(one(k) for k in keys))
    return len(keys)


async def poll() -> int:
    board = await _search(settings.jira_jql)
    seen: set[str] = set()
    for it in board:
        norm = _norm_issue(it)
        if norm:
            await _upsert_issue(norm, pinned=False)
            seen.add(norm["key"])
    pin_keys = await _pinned_keys()
    await fetch_keys(pin_keys)
    seen.update(pin_keys)
    # only prune when the board fetch actually returned (avoid wiping on a fluke)
    if board:
        await store.prune_source("jira", seen)
    return len(seen)
