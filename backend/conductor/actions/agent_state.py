"""Ground-truth agent state from live claude tmux panes.

Split out of terminal.py. The 5s pane poll is the single writer of
running/waiting (hooks only ever say "working"/"idle" — see terminal._hook_cfg):
it reads the actual screen, so it cannot miss a transition or go stale after a
restart. Lane precedence lives in lanes.py; this module only decides what each
pane says.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shlex
import time

from .. import status
from ..config import settings
from .runner import _run, reachable

log = logging.getLogger("conductor.agent_state")

# claude's status line while it's actively working carries an elapsed timer + token
# counter, e.g. "✻ Boogieing… (3m 45s · ↓ 2.0k tokens · …)", or an "esc to interrupt"
# hint. The INITIAL thinking phase shows neither yet — just the spinner-glyph gerund
# line ("✳ Wibbling…", then "✳ Caramelizing… (2s · thinking)"; captured live
# 2026-07-16), which vanished the moment the turn ended — so match that line too, or
# a fresh prompt sits in Need Human until the first tool call. Idle/permission/rating
# states drop all of these. This is ground truth on screen — unlike the edge hooks,
# it can't miss a transition or go stale after a restart.
_AGENT_RUNNING_RE = re.compile(
    r"esc to interrupt"
    r"|\((?:\d+m\s*)?\d+s\s*·[^)]*(?:token|thinking)"
    r"|^\s*[✳✻✽✷✢✶⠁-⣿]\s+[A-Z][a-z]+ing…",
    re.I | re.M,
)
# a background task (CI/monitor watcher) claude launched before returning to the prompt:
# "N shell(s) still running" / "· N shells". Its main loop is idle but the job is
# autonomously in flight and self-resumes, so it's still AI-working, NOT ball-in-court.
# "Ran N shell command" (past tense, no count) is a finished shell and does NOT match.
_BG_COUNT_RE = re.compile(r"(\d+)\s+shells?\s+still\s+running|·\s*(\d+)\s+shells?\b", re.I)
_BG_CMD_RE = re.compile(r'Background command "([^"]{2,60})')

# a LIVE choice dialog (AskUserQuestion modal / permission prompt) renders a numbered
# option list with a cursor: "❯ 1. Coffee" / "❯ 1. Yes" (live-captured 2026-07-16).
# An idle prompt is a bare "❯"; transcript lists have no cursor — so `❯ 1.` in the
# pinned bottom region means claude is waiting for YOU to pick something. The dialog
# collapses when answered, so this self-clears.
_CHOICE_RE = re.compile(r"^\s*❯\s*\d+\.", re.M)
_CHOICE_SKIP_RE = re.compile(r"^[\s─═╭╮╰╯│┌┐└┘-]*$|^\s*[☐☑✔]")


def _choice_detail(bottom: str) -> str | None:
    """The question a live choice dialog is asking — the nearest content line above
    the `❯ 1.` cursor row ("Coffee or tea?", "Do you want to proceed?") — or None
    when no dialog is up."""
    m = _CHOICE_RE.search(bottom)
    if not m:
        return None
    above = bottom[: m.start()].splitlines()
    for line in reversed(above):
        if line.strip() and not _CHOICE_SKIP_RE.match(line):
            return line.strip()[:200]
    return "Claude is asking you to choose"


def _bg_detail(text: str) -> str | None:
    """A short label for what a card's background task is doing — the watcher's command
    if still on screen, else the shell count — for display on the card."""
    m = _BG_CMD_RE.search(text)
    if m:
        return re.sub(r"\s+", " ", m.group(1)).strip().rstrip(".…")  # unwrap pane line-wraps
    m = _BG_COUNT_RE.search(text)
    if m:
        n = m.group(1) or m.group(2)
        return f"{n} shell{'s' if n != '1' else ''} running"
    return None


# --- richer per-session status for the Terminals sidebar ----------------------
# The statusline our sessions render (via the pty shim → statusline-command.sh)
# leaves a plain-text line at the pane bottom: "<dir> <branch> ~N <model> ctx:NN%
# session:NN% \"<last prompt>\"". capture-pane -p strips ANSI, so we can read it back.
_SL_MODEL_RE = re.compile(r"\b(Opus|Sonnet|Haiku|Fable)[\w.]*(?:\s+[\d.]+)?", re.I)
_CTX_RE = re.compile(r"ctx:(\d+)%")
_TASK_RE = re.compile(r'"([^"]{2,100})"')


def _pane_status(text: str) -> dict:
    """Everything worth showing for one live session, from a single pane capture:
    running vs waiting, background-task label, and (from the statusline line) the
    model, context %, and the last prompt as a task summary. Pure — testable."""
    lines = text.splitlines()
    bottom = "\n".join(lines[-14:])
    # choice dialogs are airy (options + descriptions + blank interlines): the `❯ 1.`
    # cursor row can sit well above the last 14 raw lines — give it a wider window.
    # Safe: an ANSWERED dialog collapses entirely, so `❯ 1.` never lingers in history.
    choice_bottom = "\n".join(lines[-40:])
    full = "\n".join(lines)
    running = bool(_AGENT_RUNNING_RE.search(bottom) or _BG_COUNT_RE.search(bottom))
    sl = next((ln for ln in reversed(lines) if "ctx:" in ln), "")
    ctx = _CTX_RE.search(sl)
    model = _SL_MODEL_RE.search(sl)
    task = _TASK_RE.search(sl)
    return {
        "state": "running" if running else "waiting",
        "bg": _bg_detail(full) if running else None,
        "choice": None if running else _choice_detail(choice_bottom),
        "model": model.group(0).strip() if model else None,
        "ctx_pct": int(ctx.group(1)) if ctx else None,
        "task": task.group(1) if task else None,
    }


_STATUS_CACHE: dict[tuple[str | None, str], tuple[float, dict | None]] = {}

# claude sets the terminal title (OSC) to a running summary of the task — the same
# thing Warp/cmux surface per tab. tmux keeps it as #{pane_title}. Strip claude's
# leading status glyph (✳ idle / braille-spinner ⠂ working / ✻) and drop the
# default "Claude Code" placeholder.
_TITLE_STRIP_RE = re.compile(r"^[\s✳✻✽✷·•*⏵▶◯●○◆✶⠀-⣿]+")


def _clean_summary(title: str) -> str | None:
    t = _TITLE_STRIP_RE.sub("", title).strip()
    return t[:120] if t and t.lower() != "claude code" else None


# claude's on-screen chrome — present even when our statusline shim isn't (a claude
# started by hand in a plain shell). None of these appear in a normal shell prompt.
_CLAUDE_UI_RE = re.compile(
    r"esc to interrupt|⏵⏵|\? for shortcuts|bypass permissions|"
    r"auto-accept edits|Welcome to Claude Code",
    re.I,
)


def _looks_like_claude(pane: str, st: dict) -> bool:
    """Is this pane actually a claude session, vs a plain shell? Gates whether a
    card-less ('other') tmux gets a live status dot in the Terminals sidebar — a
    hand-run claude should, a bash/vim/log-tail should not. Signals, any of: our
    statusline's ctx:/model line, an active spinner or background turn, or claude's
    own UI chrome on screen."""
    return bool(
        st.get("ctx_pct") is not None
        or st.get("model")
        or st.get("state") == "running"
        or _CLAUDE_UI_RE.search(pane)
    )


async def session_status(name: str, host: str | None = None, ttl: float = 6.0) -> dict | None:
    """Cached live status for one tmux session (None if its pane is unreadable):
    running/bg/model/ctx from the pane text + `summary` from claude's terminal
    title (#{pane_title}). Title + capture ride ONE round-trip (cheap over ssh);
    cached briefly so several viewers don't multiply the captures."""
    key = (host, name)
    now = time.monotonic()
    hit = _STATUS_CACHE.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    q = shlex.quote(name)
    tb = shlex.quote(settings.tmux_bin)
    # \036 (record separator) can't appear in a title or pane, so it's a safe split
    script = f"{tb} display -p -t {q} '#{{pane_title}}'; printf '\\036'; {tb} capture-pane -p -t {q}"
    rc, out = await _run(["sh", "-c", script], host=host)
    st = None
    if rc == 0:
        title, _sep, pane = out.decode(errors="replace").partition("\x1e")
        st = _pane_status(pane)
        st["summary"] = _clean_summary(title)
        st["is_claude"] = _looks_like_claude(pane, st)
    _STATUS_CACHE[key] = (now, st)
    return st


# last seen pane content per (host, session) — a repainting screen means SOMETHING
# is alive (thinking timer ticking, subagent-team rows updating) even when the
# classic spinner line is absent. Compared within one process only.
_PANE_SEEN: dict[tuple[str | None, str], int] = {}


async def _classify_pane(
    name: str, host: str | None = None, sustain_running: bool = False
) -> tuple[str, str | None, str | None] | None:
    """(state, bg_detail) from a live claude pane, or None if unreadable. state is
    'running' for a live foreground turn OR an in-flight background task (both AI
    Working); 'waiting' only when the main loop is idle with no background work. Both
    the live timer AND the "N shells still running" bg indicator sit in claude's pinned
    bottom status region (search `bottom`) — a COMPLETED bg command's old spinner line
    ("· N shells still running") stays frozen higher up in the pane, so searching the
    whole capture kept re-pinning a finished job to AI Working (the bug this fixes).

    The spinner regexes are UI-version-dependent and MISS newer claude states
    (extended thinking frames, the subagent-team rows `◯ agent … 47s · ↓ 53k
    tokens`). So a card that is already running is additionally SUSTAINED by
    screen activity: pane content changed since the last poll ⇒ still running.
    An idle claude's screen is static, so this can't fake AI Working — it only
    bridges frames the regex doesn't recognize."""
    rc, out = await _run([settings.tmux_bin, "capture-pane", "-p", "-t", name], host=host)
    if rc != 0:
        return None
    text = out.decode(errors="replace")
    lines = text.splitlines()
    bottom = "\n".join(lines[-14:])
    full = "\n".join(lines)
    key = (host, name)
    prev = _PANE_SEEN.get(key)
    cur = hash(text)
    _PANE_SEEN[key] = cur
    if _AGENT_RUNNING_RE.search(bottom) or _BG_COUNT_RE.search(bottom):
        return "running", _bg_detail(full), None
    # wider window than the running check: choice dialogs are airy, the `❯ 1.` row
    # can sit >14 raw lines up (see _pane_status); an answered dialog fully collapses
    choice = _choice_detail("\n".join(lines[-40:]))
    if choice:  # a live dialog trumps sustain — the screen may repaint but the ball is yours
        return "waiting", None, choice
    if sustain_running and prev is not None and prev != cur:
        return "running", _bg_detail(full), None  # screen still repainting → work in flight
    return "waiting", None, None


_Capture = tuple[str, str | None, str | None]
_PaneKey = tuple[str, str | None]


async def _capture_panes(
    panes: list[_PaneKey], sustain: dict[_PaneKey, bool]
) -> tuple[dict[_PaneKey, _Capture], list[BaseException]]:
    """Read every DISTINCT bound pane once, concurrently, capped per host.

    DEDUPED by (name, host), because one pane legitimately drives two cards: a PASSIVE
    attach_only open (api/terminals.py) deliberately leaves the other card's
    TerminalSession row in place rather than stealing it. Capturing such a pane twice
    in one gather is not merely wasteful, it is WRONG — `_PANE_SEEN` is keyed by
    (host, name), so whichever coroutine resumes second reads back the first one's hash,
    sees prev == cur, and loses the sustain_running bridge that keeps a mid-turn claude
    out of Need Human. Read sequentially the two captures landed seconds apart and a
    repainting screen sustained both.

    CAPPED per host, because each capture reaches runner._run, which spawns a tmux
    subprocess or an ssh channel with no limit of its own: a large enough session set
    exhausts processes, file descriptors or the remote's MaxSessions, and the captures
    that then fail are exactly the ones whose cards keep a stale lane. Per host rather
    than global so one slow remote cannot starve the local reads. The gates are built
    per call on purpose — an asyncio primitive cached across calls binds to the loop
    that first awaited it.

    Returns the readable captures by pane, plus the exceptions, which the caller reports
    through `status`: a systematically dead pane has to colour the health dot, not
    vanish into DEBUG.
    """
    cap = max(1, settings.agent_poll_max_concurrent)
    gates = {host: asyncio.Semaphore(cap) for _, host in panes}

    async def _one(name: str, host: str | None) -> _Capture | None:
        async with gates[host]:
            return await _classify_pane(name, host, sustain_running=sustain[(name, host)])

    results = await asyncio.gather(
        *(_one(name, host) for name, host in panes),
        return_exceptions=True,  # one unreadable pane must not sink the whole cycle
    )
    out: dict[_PaneKey, _Capture] = {}
    failures: list[BaseException] = []
    for key, result in zip(panes, results, strict=True):
        if isinstance(result, BaseException):
            log.warning("[agent-state] pane %s@%s failed: %s", key[0], key[1] or "local", result)
            failures.append(result)
        elif result:
            out[key] = result
    return out, failures


# consecutive polls a card has looked demotable (idle panes / sessions gone). A
# single quiet frame is routinely a lie — a thinking→acting transition, a pane
# redraw, one flaky `tmux ls` — so demotions need two strikes (~10s). Promotions
# stay instant.
_QUIET_POLLS: dict[str, int] = {}


async def _probe_state(
    card_id: str, sessions: set[tuple[str, str | None]], was_running: bool
) -> tuple[str, str | None] | None:
    """Ask every plugin-declared ``StateProbeSpec`` for extra evidence that one of this
    card's idle-looking sessions is actually working (e.g. an agent-team lead whose
    teammates are mid-task). All (probe × session) calls run CONCURRENTLY under one 2s
    per-card deadline — serial awaits would let a hung probe cost 2s × specs × sessions
    and stall every later card in the polling pass. Among whatever finished, the first
    ``("running", detail)`` in declaration order wins (deterministic); a probe that
    errors, overruns, or returns nothing abstains — plugin code must never stall or flip
    the poll. Called only for cards whose panes all read waiting with no live choice
    dialog, so the cost rides the quiet path, not every cycle."""
    from ..plugins import runtime  # lazy: the plugins package imports this module's siblings

    async def _one(probe, ctx: dict) -> tuple[str, str | None] | None:
        try:
            return await probe(ctx)
        except Exception as exc:  # noqa: BLE001 — abstain, whatever broke
            log.debug("[probe] %s on %s abstained with error: %s", card_id, ctx["name"], exc)
            return None

    tasks = [
        asyncio.create_task(
            _one(spec.probe, {"card_id": card_id, "name": name, "host": host, "was_running": was_running})
        )
        for _plugin_id, spec in runtime.spec_rows("state_probes")
        for name, host in sessions
    ]
    if not tasks:
        return None
    done, pending = await asyncio.wait(tasks, timeout=2.0)
    for t in pending:  # a slow probe abstains; it must not mask a fast one's verdict
        t.cancel()
    if pending:
        log.debug("[probe] card %s: %d probe(s) overran the 2s card deadline", card_id, len(pending))
    for t in tasks:  # declaration order → deterministic winner among the finished
        if t in done and (got := t.result()) and got[0] == "running":
            return got
    return None



async def poll_agent_states() -> int:
    """Authoritative agent state from the live tmux pane, correcting stale hook state.
    A session showing the working spinner (or a still-repainting screen) → AI
    Working; a live session idle for two consecutive polls → Need Human; a card
    whose sessions are gone for two polls → clear (revert to its real lane). The
    hooks give instant working transitions; this reconciles every cycle."""
    from sqlalchemy import select  # lazy: avoid an import cycle with store

    from ..db import session_maker
    from ..models import Card, TerminalSession
    from ..store import upsert_card
    from .terminal import list_tmux  # lazy: terminal imports this module (façade)

    tmuxes = await list_tmux()
    all_live = {(t.get("host"), t["name"]) for t in tmuxes}
    conductor_live = {(t.get("host"), t["name"]) for t in tmuxes if t.get("kind") == "conductor"}
    for k in [k for k in _PANE_SEEN if k not in all_live]:
        _PANE_SEEN.pop(k, None)
    # an offline remote is a blind spot, not proof the session died — hold state
    dark_hosts = {h for h in settings.remote_host_map if not await reachable(h)}
    async with session_maker() as session:
        rows = (await session.execute(select(TerminalSession))).scalars().all()
        # columns, not entities: this runs every agent_poll_s (5s) over the WHOLE card
        # table, and all four consumers below (uuid map + snapshot) read plain fields.
        # Hydrating full ORM objects for a few thousand rows cost ~25-50ms per cycle
        # buying nothing — Row exposes .id/.cached the same way.
        cards = (
            await session.execute(
                select(Card.id, Card.origin, Card.external_id, Card.cached)
            )
        ).all()
        # a tmux drives its card's agent state if it's one of our conductor sessions OR
        # a card-less session explicitly adopted onto a card (any name/kind) — both must
        # be live now. Adoption lets a hand-run claude in a shell-* session set its
        # card's AI-Working / Need-Human lane just like a conductor session.
        adopted_live = {(r.host, r.tmux_session) for r in rows if r.tmux_session} & all_live
        live = conductor_live | adopted_live
        by_card: dict[str, set[tuple[str, str | None]]] = {}
        dark_cards: set[str] = set()
        for r in rows:
            if (r.host, r.tmux_session) in live:
                by_card.setdefault(r.card_id, set()).add((r.tmux_session, r.host))
            elif r.host in dark_hosts and r.status == "live":
                dark_cards.add(r.card_id)
        snapshot = [
            (c.id, c.origin, c.external_id, dict((c.cached or {}).get("agent") or {}))
            for c in cards
        ]

    # Read every bound pane ONCE, CONCURRENTLY, before deciding anything. This used to
    # be a sequential await per session nested inside the sequential card loop below, so
    # a cycle cost the SUM of every capture: a tmux subprocess each locally, a full ssh
    # round trip (~100-300ms) per remote session, with every later card queued behind the
    # earlier ones. On a 5s poll that is the loop's whole budget. /api/tmux already
    # gathers the identical work (api/terminals.py) — this brings the hot path in line.
    was_running_by_card = {cid: bool(agent.get("running")) for cid, _, _, agent in snapshot}
    # each card's panes in a stable order (host may be None, so never compare it raw):
    # the bg-detail pick below takes the FIRST running result, so this ordering is what
    # keeps that choice the same across cycles. Cards are walked in id order too, so the
    # deduped dispatch list is itself deterministic.
    card_panes: dict[str, list[_PaneKey]] = {
        cid: sorted(sess, key=lambda s: (s[0], s[1] or ""))
        for cid, sess in sorted(by_card.items())
    }
    # one pane, several cards → sustain if ANY holder was running. The bridge only ever
    # promotes, and a repainting pane IS work in flight for every card bound to it, so
    # the union is the honest read of a shared pane.
    sustain: dict[_PaneKey, bool] = {}
    for cid, keys in card_panes.items():
        for key in keys:
            sustain[key] = sustain.get(key, False) or was_running_by_card.get(cid, False)
    captured, capture_failures = await _capture_panes(list(sustain), sustain)
    # the capture failures ARE the health signal for this poller. Swallowed at DEBUG they
    # left `agent-state` green while every pane read was dying (a renamed tmux_bin or a
    # broken ssh raises out of _run rather than returning non-zero), and a card pinned at
    # running=True then sat in AI Working forever with nothing above DEBUG to say why.
    # Its own /api/status entry, because _run_poller's record_ok fires on the way out of
    # this function and would clear an error recorded under "agent-state".
    if capture_failures:
        status.record_error(
            "agent-state-panes",
            RuntimeError(
                f"{len(capture_failures)}/{len(sustain)} pane capture(s) failed, "
                f"first: {type(capture_failures[0]).__name__}: {capture_failures[0]}"
            ),
        )
    else:
        status.record_ok("agent-state-panes")
    panes = {
        cid: [c for key in keys if (c := captured.get(key))] for cid, keys in card_panes.items()
    }

    # Read what the panes say for EVERY card before any probe runs, then run the probes
    # concurrently too. `_probe_state` waits up to 2s per card; awaited one at a time
    # inside the decision loop, a later card's snapshot aged behind every earlier card's
    # probe (2s × probing cards) and could be decided from a pane that had since started
    # working or raised a dialog. One gather bounds that to a single 2s window, the same
    # for every card, so the loop below is pure logic over text of uniform freshness.
    reads: dict[str, tuple[list[_Capture], bool, str | None]] = {}
    for cid, results in panes.items():
        if not results:
            continue  # couldn't read any pane → leave as-is
        running = any(st == "running" for st, _, _ in results)
        # a live choice dialog (question/permission) waiting on the human — demote
        # instantly (no two-strike): the dialog on screen IS the proof
        choice = next((c for _, _, c in results if c), None) if not running else None
        reads[cid] = (results, running, choice)
    # quiet panes + no dialog: plugin state probes may know better (an agent-team lead
    # idles at its prompt while teammates work). A probe can only promote — a choice
    # dialog outranks it, claude is literally asking the human.
    quiet_cards = [cid for cid, (_, running, choice) in reads.items() if not (running or choice)]
    # contained like the capture gather above: `_probe_state` guards the probe CALL, but
    # anything raised around it (the lazy runtime import, task construction, or a probe
    # that ends CancelledError — which `except Exception` inside it does not catch and
    # `t.result()` re-raises) would otherwise escape this gather. An Exception costs the
    # whole cycle for every card; a CancelledError is worse, since _run_poller re-raises
    # it and the poller never comes back. A card whose probe blew up simply abstains.
    # NOTE the exception results must be dropped, not just tolerated: a BaseException is
    # TRUTHY, so leaving one in would make `if probed` below promote the card to Running
    # off a crash, then `probed[1]` would TypeError on the detail.
    probed_by_card: dict[str, tuple[str, str | None]] = {}
    for cid, probed in zip(
        quiet_cards,
        await asyncio.gather(
            *(_probe_state(cid, by_card[cid], was_running_by_card[cid]) for cid in quiet_cards),
            return_exceptions=True,
        ),
        strict=True,
    ):
        if isinstance(probed, BaseException):
            log.warning("[agent-state] probe for card %s failed: %s", cid, probed)
        elif probed:
            probed_by_card[cid] = probed

    changed = 0
    for cid, origin, eid, agent in snapshot:
        sessions = by_card.get(cid)
        was_running = was_running_by_card[cid]
        if sessions:
            read = reads.get(cid)
            if not read:
                continue  # couldn't read any pane → leave as-is
            results, running, choice = read
            probed = probed_by_card.get(cid)
            if probed:
                running = True
            if running or choice:
                _QUIET_POLLS.pop(cid, None)
            else:
                quiet = _QUIET_POLLS.get(cid, 0) + 1
                _QUIET_POLLS[cid] = quiet
                if was_running and quiet < 2:
                    continue  # one quiet frame proves nothing — two strikes to demote
            bg = next((d for st, d, _ in results if st == "running" and d), None) if running else None
            if probed and not bg:
                bg = probed[1]  # the probe's label ("team 3/8 tasks") — the panes offered none
            new = {"active": True, "running": running, "waiting": not running, "bg": bg, "choice": choice}
        elif agent.get("active") or agent.get("running") or agent.get("waiting"):
            # any lingering live-ish flag with no session → stale, clear it. Checking
            # running/waiting too (not just active) heals an inconsistent state where
            # active got dropped but running/waiting stuck (would pin AI Working forever).
            if cid in dark_cards:
                continue  # its host is offline — can't observe, don't flip the lane
            quiet = _QUIET_POLLS.get(cid, 0) + 1
            _QUIET_POLLS[cid] = quiet
            if quiet < 2:
                continue  # a single flaky `tmux ls` must not mass-clear the board
            # no session → clear everything, including any stale Notification message
            new = {
                "active": False, "running": False, "waiting": False,
                "bg": None, "choice": None, "notification": None,
            }
        else:
            _QUIET_POLLS.pop(cid, None)
            continue
        cur = {
            "active": bool(agent.get("active")), "running": bool(agent.get("running")),
            "waiting": bool(agent.get("waiting")), "bg": agent.get("bg"),
            "choice": agent.get("choice"),
        }
        if cur == new:
            continue
        async with session_maker() as s:
            await upsert_card(s, origin=origin, external_id=eid, cached_patch={"agent": new})
        changed += 1
    return changed

