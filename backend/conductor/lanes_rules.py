"""The 17 recompute_ball rules as kernel waterfall handlers (M4).

lanes.py's precedence if-chain becomes 17 subscriptions on :class:`BallEvent`
at priorities 10, 20, … 190 (the 80 and 120 slots were retired with their
rules) — same order, same verdicts, dispatched through
``kernel.events.waterfall_sync`` (first non-None return wins). Core is the
seam's own first customer: these are core's DEFAULT providers, registered in a
kernel scope at import, and a plugin can now insert a rule at any intermediate
priority (e.g. 55: above claims, below a running conductor session) without an
edit here — the capability the old closed if-chain could not offer.

The behavioral contract is ``tests/test_lanes_pinning.py`` (written against
the pre-kernel chain, unmodifiable): ANY divergence from it is a defect, and
that includes the owner's explicit precedence rulings baked into the order —
notably rule 6, a foreign claim outranking ``agent.waiting`` (a past owner
decision that survived a CodeRabbit challenge).

Waterfall fine print: when every handler abstains the bus hands the EVENT back
(so transform-only chains keep their work) — a caller must never mistake that
for a verdict. :func:`verdict_of` is the one classification point: verdicts
here are ``(ball, agent_state)`` tuples, never Events, so an isinstance check
is exact. In practice the last rule (priority 190) always returns a verdict;
:func:`default_verdict` doubles as the zero-handler fallback in lanes.py.

Rules live in their own module (not lanes.py) so the dependency arrow stays
acyclic: this module imports only the kernel; lanes.py imports this module and
re-exports ``DONE_JIRA_STATUSES`` for its existing importers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .kernel import CompositeEffect, Event, kernel

# Jira statuses that mean "no longer needs anyone" → Done lane.
DONE_JIRA_STATUSES = {
    "Done",
    "Closed",
    "Resolved",
    "Cancelled",
    "Canceled",
    "Won't Do",
    "Merged",
    "Released",
}

#: A verdict is (ball, agent_state) — ball ∈ human | ai | none.
Verdict = tuple[str, "str | None"]


@dataclass(frozen=True)
class BallEvent(Event):
    """One 'who holds the ball?' evaluation over a card's merged ``cached``.

    Frozen (repo idiom): a rule that wants to normalize the payload for later
    rules returns ``Transform(replace(event, ...))`` instead of mutating what
    an earlier rule already read."""

    cached: dict
    hold_label: str = "conductor-hold"
    origin: str | None = None


_BALLS = frozenset({"human", "ai", "none"})


class MalformedVerdictError(TypeError):
    """An injected lane rule returned something that is neither an abstain
    (the dispatched BallEvent handed back) nor a valid ``(ball, agent_state)``
    verdict. LOUD by design: this seam exists for third-party rules, and a
    buggy one must fail at the dispatch boundary — a bare string would
    otherwise unpack as ball=<first char> (silent lane corruption), and a
    FOREIGN Event type would be misread as all-abstained (silent hijack of
    the default verdict)."""


def verdict_of(result: Any) -> Verdict | None:
    """Classify a waterfall return. None ⇒ no rule fired — that case hands
    the (possibly Transform-replaced) BallEvent back, and ONLY a BallEvent
    counts as an abstain: any other Event type is just a rule's return value
    and must pass verdict validation or die loudly. A valid verdict is a
    2-tuple whose ball ∈ human|ai|none and whose agent_state is a str or
    None; anything else raises :class:`MalformedVerdictError` (see its
    docstring for why raising, not skipping, is the chosen failure mode)."""
    if isinstance(result, BallEvent):
        return None
    if (
        isinstance(result, tuple)
        and len(result) == 2
        and result[0] in _BALLS
        and (result[1] is None or isinstance(result[1], str))
    ):
        return result
    raise MalformedVerdictError(
        f"lane rule returned {result!r}; expected the BallEvent back (abstain) "
        f"or a (ball, agent_state) tuple with ball in {sorted(_BALLS)}"
    )


# ── shared prelude helpers ────────────────────────────────────────────────────


def _sig(e: BallEvent) -> dict:
    """The card's OWN origin namespace's generic signal — a plugin-provided
    source drives ball through ``cached.<origin>.signal = {state, detail}``
    instead of an edit here. Only the card's own origin is read; a foreign
    namespace's signal must go through claims (working) or signals (needs_me)."""
    return ((e.cached.get(e.origin) or {}).get("signal") or {}) if e.origin else {}


def _claim(e: BallEvent) -> dict:
    """A FOREIGN claim: a plugin reporting that something it owns is working
    THIS card (``cached.claims.<plugin> = {state, detail}``). FIRST truthy-
    ``state`` value wins the election — even a non-"working" state — matching
    the legacy chain byte for byte (pinned)."""
    return next(
        (c for c in (e.cached.get("claims") or {}).values() if isinstance(c, dict) and c.get("state")),
        {},
    )


# ── the 17 rules, priorities 10…190 (80 and 120 retired) ─────────────────────
# Terminal states win over EVERYTHING — a Done/merged/handled ticket is
# finished no matter what a lingering hold label or stale waiting flag says.


def _rule_010_terminal(e: BallEvent) -> Verdict | None:
    jira = e.cached.get("jira") or {}
    gh = e.cached.get("github") or {}
    if jira.get("status") in DONE_JIRA_STATUSES or gh.get("state") in ("merged", "closed"):
        return "none", jira.get("status") or gh.get("state")
    return None


def _rule_020_slack_done(e: BallEvent) -> Verdict | None:
    if (e.cached.get("slack") or {}).get("done"):
        return "none", "done"
    return None


def _rule_030_manual_closed(e: BallEvent) -> Verdict | None:
    # a manual note explicitly marked done (open=False) is finished too.
    # `is False` distinguishes an explicitly closed note from a non-manual
    # card (no `manual` key → .get() is None).
    if (e.cached.get("manual") or {}).get("open") is False:
        return "none", "done"
    return None


def _rule_040_signal_done(e: BallEvent) -> Verdict | None:
    sig = _sig(e)
    if sig.get("state") == "done":  # generic terminal — same rank as built-ins
        return "none", sig.get("detail") or "done"
    return None


def _rule_050_agent_running(e: BallEvent) -> Verdict | None:
    # a dashboard-launched Claude is driving this card (highest live
    # precedence). A background task still counts as working — surface what
    # it's watching.
    agent = e.cached.get("agent") or {}
    if agent.get("running"):
        bg = agent.get("bg")
        return "ai", f"⏵ {bg}" if bg else "claude · working"
    return None


def _rule_060_claim_working(e: BallEvent) -> Verdict | None:
    # A plugin's agent is working this card. Ranked above every human-needed
    # signal, below only the terminal states and a conductor session that is
    # itself running — an idle `waiting` session means THAT session has
    # nothing to do, not that YOU do. It therefore also outranks a claude
    # asking a question (OWNER RULING — the ask still renders on the card).
    claim = _claim(e)
    if claim.get("state") == "working":
        return "ai", claim.get("detail") or "working"
    return None


def _rule_070_agent_waiting(e: BallEvent) -> Verdict | None:
    if (e.cached.get("agent") or {}).get("waiting"):
        return "human", "needs your input"
    return None


def _rule_090_hold_label(e: BallEvent) -> Verdict | None:
    # an external automation tool parked the ticket for a human decision
    # (the hero signal)
    if e.hold_label in ((e.cached.get("jira") or {}).get("labels") or []):
        return "human", "awaiting input"
    return None


def _rule_100_manual_note(e: BallEvent) -> Verdict | None:
    # manual note the user created → always theirs until closed
    if (e.cached.get("manual") or {}).get("open"):
        return "human", "note"
    return None


def _rule_110_review_requested(e: BallEvent) -> Verdict | None:
    gh = e.cached.get("github") or {}
    if (
        gh.get("review_requested_me")
        and not gh.get("reviewed_by_me")
        and gh.get("state", "open") == "open"
    ):
        return "human", "review requested"
    return None


def _rule_130_signal_working(e: BallEvent) -> Verdict | None:
    sig = _sig(e)
    if sig.get("state") == "working":  # generic ai-working signal from any source
        return "ai", sig.get("detail") or "working"
    return None


def _rule_140_open_pr(e: BallEvent) -> Verdict | None:
    # an open PR card I'm tracking → mine to review/merge (review-requested
    # PRs already caught at rule 11; merged/closed returned "none" at rule 1).
    gh = e.cached.get("github") or {}
    if gh.get("state") == "open" and gh.get("number") is not None:
        ci = gh.get("ci")
        label = "ci failing" if ci == "failing" else "ready to merge" if ci == "passing" else "review / merge"
        return "human", label
    return None


def _rule_150_assignee_me(e: BallEvent) -> Verdict | None:
    jira = e.cached.get("jira") or {}
    if jira.get("assignee_me"):
        return "human", jira.get("status") or "assigned"
    return None


def _rule_160_slack_unread(e: BallEvent) -> Verdict | None:
    slack = e.cached.get("slack") or {}
    if slack.get("unread"):
        return "human", f"slack {slack.get('kind') or ''}".strip()
    return None


def _rule_170_signal_needs_me(e: BallEvent) -> Verdict | None:
    sig = _sig(e)
    if sig.get("state") == "needs_me":  # generic mine-to-act-on
        return "human", sig.get("detail") or "needs you"
    return None


def _rule_180_enrich_signals(e: BallEvent) -> Verdict | None:
    # PROMOTE-ONLY enrichment signals — a plugin flagging ANOTHER source's
    # card as needing the human. May only pull a card TOWARD human, and slots
    # here, below every live/owning signal, so it can't reorder the chain.
    # Deterministic among contributors: first by name.
    signals = e.cached.get("signals") or {}
    for name in sorted(signals):
        extra = signals.get(name)
        if isinstance(extra, dict) and extra.get("state") == "needs_me":
            return "human", extra.get("detail") or f"{name}: needs you"
    return None


def default_verdict(e: BallEvent) -> Verdict:
    """Rule 19 — nothing needs anyone. Also lanes.py's fallback for the
    zero-handler / all-abstained waterfall outcome, so a wiped kernel can
    never leave recompute_ball without an answer."""
    return "none", (e.cached.get("jira") or {}).get("status") or _sig(e).get("detail")


#: The chain, in precedence order. Priorities are spaced by 10 so a plugin can
#: interpose (e.g. 55 = above claims, below a running conductor session).
CORE_RULES: tuple[tuple[int, Callable[[BallEvent], Any]], ...] = (
    (10, _rule_010_terminal),
    (20, _rule_020_slack_done),
    (30, _rule_030_manual_closed),
    (40, _rule_040_signal_done),
    (50, _rule_050_agent_running),
    (60, _rule_060_claim_working),
    (70, _rule_070_agent_waiting),
    (90, _rule_090_hold_label),
    (100, _rule_100_manual_note),
    (110, _rule_110_review_requested),
    (130, _rule_130_signal_working),
    (140, _rule_140_open_pr),
    (150, _rule_150_assignee_me),
    (160, _rule_160_slack_unread),
    (170, _rule_170_signal_needs_me),
    (180, _rule_180_enrich_signals),
    (190, default_verdict),  # rule 19: always answers, ends every dispatch
)

#: Core's registration scope. Held so the footprint is one disposable unit
#: like any plugin's; replaced wholesale on re-registration after a reset.
_core_scope: CompositeEffect | None = None


def ensure_core_rules() -> None:
    """Register the core chain on the shared kernel — idempotent, and self-
    healing after a test's ``reset_kernel()`` (same doctrine as the lazily
    re-provided registries in plugins/runtime.py: reset yields a blank bus,
    the next evaluation re-provides core's defaults). Presence is checked by
    handler identity, not emptiness, so an injected plugin rule on an
    otherwise-blank bus still gets the core chain beneath it."""
    global _core_scope
    if any(s.handler is _rule_010_terminal for s in kernel.events.subscriptions(BallEvent)):
        return
    scope = kernel.scope()
    for priority, handler in CORE_RULES:
        scope.add(
            kernel.events.subscribe(BallEvent, handler, priority=priority, modes=("waterfall",))
        )
    _core_scope = scope


# Core is its own first registrant: the default providers exist from import.
ensure_core_rules()
