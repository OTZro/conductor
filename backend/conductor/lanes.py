from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from .kernel import kernel
from .lanes_rules import (
    DONE_JIRA_STATUSES,  # noqa: F401 — re-exported: importers predate lanes_rules
    BallEvent,
    default_verdict,
    ensure_core_rules,
    verdict_of,
)

log = logging.getLogger("conductor.lanes")


def recompute_ball(
    cached: dict, *, hold_label: str = "conductor-hold", origin: str | None = None
) -> tuple[str, str | None]:
    """Single source of truth for who holds the ball, derived from the merged
    per-source signals in ``cached``. Order encodes precedence.

    Returns (ball, agent_state) where ball is human | ai | none.

    A PLUGIN-PROVIDED source drives ball through the GENERIC SIGNAL VOCABULARY
    instead of an edit here: it writes ``cached.<origin>.signal = {"state":
    "done" | "working" | "needs_me", "detail": "<status text>"}`` via upsert_card,
    and this function reads the card's OWN origin namespace at the matching
    precedence slot (done → terminal, working → ai beside a live claim, needs_me →
    human beside assignee). ``detail`` becomes agent_state — the card's status
    sentence (the FE falls back to it for unknown origins). Built-in namespaces
    keep their richer bespoke signals; a built-in card has no ``signal`` key, so
    behavior there is unchanged.

    Since M4 the chain itself lives in :mod:`conductor.lanes_rules` as 17
    kernel waterfall handlers on :class:`BallEvent` (priorities 10…190, first
    verdict wins) — same rules, same order, behavior pinned by
    tests/test_lanes_pinning.py. This wrapper keeps the historical signature
    and its synchronous nature (store's upsert path calls it inline) while a
    plugin may interpose a rule at any intermediate priority via
    ``kernel.events.subscribe(BallEvent, rule, priority=…)``."""
    ensure_core_rules()  # self-heal after a test's reset_kernel()
    event = BallEvent(cached=cached, hold_label=hold_label, origin=origin)
    # Waterfall fine print: all-abstained returns the (possibly Transform-
    # replaced) EVENT back, not a verdict — verdict_of() tells them apart and
    # raises MalformedVerdictError on anything else (a buggy injected rule
    # must fail loud, never corrupt a lane). With core registered, rule 19
    # always answers; the fallback below covers only a kernel wiped mid-
    # flight, and it feeds the RETURNED event so a transform-only chain's
    # normalization work reaches the default rule exactly as rule 19 would
    # have seen it.
    result = kernel.events.waterfall_sync(event)
    verdict = verdict_of(result)
    return default_verdict(result) if verdict is None else verdict


def lane_for(ball: str) -> str:
    return {"human": "need_human", "ai": "ai_working", "none": "done"}.get(ball, "done")


# ── stage registry ────────────────────────────────────────────────────────────────
# The board's lane set, merged from three sources (first wins on key collision):
#   1. the four BUILT-INS below (their placement logic stays imperative code),
#   2. the user's ~/.conductor/lanes.json — custom stages with no code,
#   3. plugin-contributed StageSpecs — programmatic stages.
# Every entry: {key, title, tone, order, rank, within, aging, collapsible, builtin}.
# `within` names the ball space a stage subdivides; ball itself (recompute_ball) is
# never overridable — that invariant is what keeps arbitration sane. v1 admits only
# within="human" for custom stages; the field is read from day one so opening the ai
# space later (team-pipeline columns) is a guard removal, not a redesign.
# `order` = board column position; `rank` = flat-list sort priority (they differ for
# the built-ins: the list wants need_human, backlog, ai_working, done).

BUILTIN_LANES: tuple[dict, ...] = (
    {"key": "need_human", "title": "Need Human", "tone": "human", "order": 0, "rank": 0,
     "within": "human", "aging": True, "collapsible": False, "builtin": True},
    {"key": "ai_working", "title": "AI Working", "tone": "ai", "order": 10, "rank": 20,
     "within": "ai", "aging": False, "collapsible": False, "builtin": True},
    {"key": "backlog", "title": "Backlog", "tone": "backlog", "order": 20, "rank": 10,
     "within": "human", "aging": False, "collapsible": True, "builtin": True},
    {"key": "done", "title": "Done", "tone": "done", "order": 30, "rank": 30,
     "within": None, "aging": False, "collapsible": True, "builtin": True},
)

_BUILTIN_KEYS = {lane["key"] for lane in BUILTIN_LANES}
_TONES = {"human", "ai", "done", "backlog"}
_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_ALLOWED_WITHIN = {"human"}  # v1 — widen to {"human", "ai"} when ai-space stages open


def _normalize_stage(raw: dict, source: str, taken: set[str]) -> dict | None:
    """Validate one custom-stage declaration → a registry entry, or None (warn+skip).
    Skipping (not raising) keeps a half-edited lanes.json / a sloppy plugin from taking
    the board down — invalid stages just don't exist."""
    key = raw.get("key")
    if not isinstance(key, str) or not _KEY_RE.match(key):
        log.warning("[lanes] %s: bad stage key %r — skipped", source, key)
        return None
    if key in _BUILTIN_KEYS or key in taken:
        log.warning("[lanes] %s: stage %r collides with an existing lane — skipped", source, key)
        return None
    within = raw.get("within", "human")
    if within not in _ALLOWED_WITHIN:
        log.warning(
            "[lanes] %s: stage %r within=%r not supported yet (v1 allows only 'human') — skipped",
            source, key, within,
        )
        return None
    tone = raw.get("tone", "backlog")
    if tone not in _TONES:
        log.warning("[lanes] %s: stage %r unknown tone %r — using 'backlog'", source, key, tone)
        tone = "backlog"
    order = raw.get("order", 5)
    rank = raw.get("rank", None)
    if not isinstance(order, int):
        order = 5
    if rank is not None and not isinstance(rank, int):
        rank = None
    return {
        "key": key,
        "title": str(raw.get("title") or key),
        "tone": tone,
        "order": order,
        "rank": order if rank is None else rank,
        "within": within,
        "aging": bool(raw.get("aging", False)),
        "collapsible": bool(raw.get("collapsible", False)),
        "builtin": False,
    }


class _ConfigStages:
    """lanes.json, cached by mtime with last-known-good semantics (same doctrine as
    prompts.py): a corrupt save must not silently drop the user's stages — cards would
    visibly jump back to their derived lanes. Deleting the file is the deliberate way
    to remove the layer."""

    def __init__(self) -> None:
        self._path: Path | None = None
        self._mtime: float | None = None
        self._bad_mtime: float | None = None
        self._good: list[dict] = []

    def read(self, path: Path) -> list[dict]:
        if path != self._path:  # settings.lanes_file changed (tests) → cache is void
            self._path, self._mtime, self._bad_mtime, self._good = path, None, None, []
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            self._mtime, self._good = None, []
            return []
        except OSError:
            return self._good
        if mtime == self._mtime:
            return self._good
        try:
            raw = json.loads(path.read_text())
            stages = raw.get("stages") if isinstance(raw, dict) else None
            if not isinstance(stages, list):
                raise ValueError('top level must be {"stages": [...]}')
        except (OSError, ValueError) as e:
            if mtime != self._bad_mtime:
                self._bad_mtime = mtime
                log.warning("[lanes] %s unreadable (%s) — keeping the last good stages", path, e)
            return self._good
        taken: set[str] = set()
        out: list[dict] = []
        for entry in stages:
            norm = _normalize_stage(entry if isinstance(entry, dict) else {}, str(path), taken)
            if norm:
                taken.add(norm["key"])
                out.append(norm)
        self._mtime, self._bad_mtime, self._good = mtime, None, out
        return out


_config_stages = _ConfigStages()


def _plugin_stages(taken: set[str]) -> list[dict]:
    """StageSpecs from every registrant — kernel rows plus discovered plugins, via
    the ``spec_rows`` read-through — validated through the same normalizer. Imported
    lazily — plugins import this module's consumers (store), so a top-level import
    would cycle. Read live (not cached): the registries are small and tests inject."""
    from .plugins import runtime

    out: list[dict] = []
    for plugin_id, spec in runtime.spec_rows("stages"):
        raw = {
            "key": spec.key, "title": spec.title, "within": spec.within,
            "tone": spec.tone, "order": spec.order, "rank": spec.rank,
            "aging": spec.aging, "collapsible": spec.collapsible,
        }
        norm = _normalize_stage(raw, f"plugin:{plugin_id}", taken)
        if norm:
            taken.add(norm["key"])
            out.append(norm)
    return out


def stage_registry() -> list[dict]:
    """The full merged lane set, display-ordered. Built-ins always present; config and
    plugin stages appended subject to validation."""
    from .config import settings  # late: lanes is imported at module-load time widely

    merged = [dict(lane) for lane in BUILTIN_LANES]
    taken = set(_BUILTIN_KEYS)
    config = _config_stages.read(settings.lanes_file)
    for entry in config:
        taken.add(entry["key"])
        merged.append(dict(entry))
    merged.extend(_plugin_stages(taken))
    merged.sort(key=lambda lane: (lane["order"], lane["key"]))
    return merged


def stage_entry(key: str, *, registry: list[dict] | None = None) -> dict | None:
    """One registry entry by key, or None.

    Scanned, not mapped: registries hold a handful of entries and this sits on the
    per-card arbitration path, so a dict build per call costs more than it saves.
    First match wins. For a registry this module built that is also the ONLY match —
    ``_normalize_stage`` rejects any key colliding with a builtin or an already-taken
    one — but a caller-supplied list has been through no such check, so treat
    first-match as the rule rather than uniqueness as a guarantee."""
    lanes = stage_registry() if registry is None else registry
    return next((lane for lane in lanes if lane.get("key") == key), None)


# Jira statusCategory keys / names that mean "not started" → Backlog lane.
_BACKLOG_CATEGORIES = {"new", "to do"}
_BACKLOG_STATUS_NAMES = {
    "To Do",
    "Backlog",
    "Open",
    "Reopened",
    "Selected for Development",
    "Triage",
    "New",
}


def _is_backlog(cached: dict) -> bool:
    jira = cached.get("jira") or {}
    cat = (jira.get("status_category") or "").strip().lower()
    if cat:
        return cat in _BACKLOG_CATEGORIES
    return (jira.get("status") or "") in _BACKLOG_STATUS_NAMES


def lane_for_card(
    cached: dict,
    ball: str,
    hold_label: str = "conductor-hold",
    manual_stage: str | None = None,
    *,
    registry: list[dict] | None = None,
) -> str:
    """Board lane for a card. The INVARIANT: ball derivation always wins — a done or
    AI-held card lands in its derived lane no matter what any stage override says.
    Custom stages (manual or plugin-claimed) only re-bucket cards INSIDE the human
    space, which is what keeps arbitration decidable.

    Inside the human space, precedence:
      1. ``manual_stage`` (the user's explicit move, LocalState) — the human's park
         decision beats EVERYTHING human-space, including a live waiting claude:
         "I know it needs me, I'm deferring it" is exactly what parking means, and the
         waiting signal still surfaces on the card itself (amber banner, status line,
         the notification push) — only the column changes.
      2. a live dashboard claude blocked on you → need_human. Still beats robot claims
         — a plugin's automated placement must not hide a human-blocking prompt.
      3. plugin claims (``cached.stages`` = {claimant: stage_key}, written via
         upsert_card) — when several claim, the earliest display-ordered claimed stage
         wins (deterministic, registry-defined).
      4. the built-in derivation: the hold label → need_human, not-started jira →
         backlog, else need_human.
    Unknown / deleted / wrong-space stage keys are ignored (fall through), so a stale
    override degrades to the derived lane instead of erroring.

    ``registry`` lets a caller hand in an already-built stage_registry() instead of
    having one rebuilt per call. Building it stats lanes.json, re-normalizes every
    plugin StageSpec, then merges and sorts — cheap once, but this runs PER CARD when
    a board is serialized and twice per upsert. Beyond the saving, passing one snapshot
    declares the UNIT OF WORK the lane set is frozen over: a whole response, or a whole
    old/new transition test. Plain memoization would make the rebuild cheap without
    expressing that — it would still hand card #1 and card #2000 different registries
    the moment lanes.json is saved mid-serialize. (An ambient scope, e.g. a ContextVar,
    could express it and would fail safe rather than opt-in; the explicit parameter was
    kept because this is a single-process localhost app and an argument is easier to
    follow than ambient state. That trade is worth revisiting if the call sites spread.)
    Keyword-only so it can't be mis-slotted into ``manual_stage``. Omit it and behavior
    is exactly as before.

    NOTE for callers hoisting a snapshot: this returns for ball ai/none BEFORE reading
    the registry, so building one for a card that isn't ball=human is pure waste."""
    if ball == "ai":
        return "ai_working"  # v1: within="ai" stages are guarded off in the registry
    if ball == "none":
        return "done"
    # ball == human
    lanes = stage_registry() if registry is None else registry
    if manual_stage:
        entry = stage_entry(manual_stage, registry=lanes)
        if entry and entry.get("within") == "human":
            return manual_stage
    if (cached.get("agent") or {}).get("waiting"):
        return "need_human"  # a dashboard Claude is waiting on you → act now
    claims = {
        v for v in (cached.get("stages") or {}).values() if isinstance(v, str)
    }
    if claims:
        # .get, not [...]: `registry` is caller-supplied now, so a hand-built entry
        # missing a key must fall through to the derived lane like any unknown stage.
        # The manual branch above already read it that way; leaving this one subscripted
        # meant the same partial entry returned need_human there and raised KeyError
        # here — a crash in the board's placement authority, reachable from the public
        # parameter this change introduced.
        for entry in lanes:  # display order = claim precedence
            if entry.get("within") == "human" and entry.get("key") in claims:
                return entry["key"]
    jira = cached.get("jira") or {}
    if hold_label in (jira.get("labels") or []):
        return "need_human"  # an external tool parked it → act now
    if jira and _is_backlog(cached):
        return "backlog"
    return "need_human"
