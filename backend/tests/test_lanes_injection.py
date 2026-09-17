"""The capability M4 buys: a kernel-registered lane rule at an intermediate
priority changes recompute_ball's outcome with zero core edits — and disposing
its Effect restores baseline exactly. Priority 55 sits between agent.running
(50) and foreign claims (60), a slot the old closed if-chain could not offer.
"""

from __future__ import annotations

from conductor.kernel import kernel
from conductor.lanes import recompute_ball
from conductor.lanes_rules import BallEvent


def test_injected_rule_at_55_interposes_then_disposes_cleanly():
    claimed = {"claims": {"plug": {"state": "working", "detail": "plug busy"}}}
    running = {"agent": {"running": True},
               "claims": {"plug": {"state": "working", "detail": "plug busy"}}}

    # baseline: rule 6 (claims) answers
    assert recompute_ball(claimed) == ("ai", "plug busy")

    def freeze_claimed_cards(e: BallEvent):
        if e.cached.get("claims"):
            return "human", "frozen by policy plugin"
        return None  # abstain otherwise

    effect = kernel.events.subscribe(
        BallEvent, freeze_claimed_cards, priority=55, modes=("waterfall",)
    )
    try:
        # the injected rule outranks claims (60)…
        assert recompute_ball(claimed) == ("human", "frozen by policy plugin")
        # …but not a running conductor session (50): priorities are honored
        assert recompute_ball(running) == ("ai", "claude · working")
    finally:
        effect.dispose()

    # disposal restores baseline exactly
    assert recompute_ball(claimed) == ("ai", "plug busy")
    assert recompute_ball(running) == ("ai", "claude · working")


# ── injection-path hardening: buggy third-party rules fail LOUD ───────────────


def test_malformed_string_verdict_raises_instead_of_unpacking():
    import pytest

    from conductor.lanes_rules import MalformedVerdictError

    effect = kernel.events.subscribe(
        BallEvent, lambda e: "hi", priority=55, modes=("waterfall",)
    )
    try:
        # without validation "hi" would unpack as ball='h'/agent_state='i'
        with pytest.raises(MalformedVerdictError):
            recompute_ball({"claims": {"p": {"state": "working"}}})
    finally:
        effect.dispose()
    # disposal restores baseline
    assert recompute_ball({"claims": {"p": {"state": "working"}}}) == ("ai", "working")


def test_foreign_event_verdict_raises_instead_of_reading_as_abstain():
    import pytest

    from conductor.kernel import Event
    from conductor.lanes_rules import MalformedVerdictError

    class ForeignEvent(Event):
        pass

    effect = kernel.events.subscribe(
        BallEvent, lambda e: ForeignEvent(), priority=55, modes=("waterfall",)
    )
    try:
        # a foreign Event WINS the waterfall; only a BallEvent means abstain,
        # so this must raise, not silently fall back to the default verdict
        with pytest.raises(MalformedVerdictError):
            recompute_ball({"claims": {"p": {"state": "working"}}})
    finally:
        effect.dispose()
    assert recompute_ball({"claims": {"p": {"state": "working"}}}) == ("ai", "working")


def test_all_abstain_fallback_sees_the_transformed_event(monkeypatch):
    """Zero core handlers + a transform-only chain: the fallback default must
    read the TRANSFORMED event (what rule 19 would have seen), not the
    original one."""
    from dataclasses import replace

    from conductor.kernel import Transform, reset_kernel
    from conductor import lanes

    reset_kernel()
    try:
        # keep recompute_ball from re-providing the core chain for this test
        monkeypatch.setattr(lanes, "ensure_core_rules", lambda: None)

        def normalize(e: BallEvent):
            return Transform(replace(e, cached={"jira": {"status": "Normalized"}}))

        effect = kernel.events.subscribe(
            BallEvent, normalize, priority=5, modes=("waterfall",)
        )
        try:
            # default_verdict over the transformed cached → its jira.status
            assert recompute_ball({}) == ("none", "Normalized")
        finally:
            effect.dispose()
        # zero handlers at all: plain default over the original event
        assert recompute_ball({"jira": {"status": "Blocked"}}) == ("none", "Blocked")
    finally:
        reset_kernel()  # blank bus; the next real caller re-provides core
