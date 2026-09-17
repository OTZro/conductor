"""Behavioral pin for lanes.recompute_ball — the M4 refactor contract.

Written against the CURRENT (pre-kernel) implementation and green there; the
kernel-waterfall conversion must keep every case green WITHOUT editing this
file. Pins, per the M0 map (section 4a):

  * each of the 17 rules firing alone (with its detail/fallback variants),
  * full pairwise precedence — every rule beats every LOWER rule when both
    genuinely fire on one merged fixture (auto-generated from per-rule
    fragments; mutually-exclusive pairs are excluded, not fudged),
  * owner precedence rulings baked into the chain — notably rule 6: a foreign
    claim outranks agent.waiting (survived a CodeRabbit challenge),
  * own-origin signal isolation (sig is read ONLY from the card's own origin),
  * claims selection = FIRST truthy-`state` value, not first `working` one,
  * the DONE_JIRA_STATUSES set, member by member,
  * lane_for(ball) mapping incl. the unknown-ball default.

This file is the contract: after phase 1 it is UNMODIFIABLE (the 2026-09
removal of two rules tied to a deleted external-automation integration —
own-tracker live activity and its running-flag — is the one deliberate
exception: those rules and their fixtures are gone, and every remaining rule
was renumbered down to close the gap)."""

from __future__ import annotations

import pytest

from conductor.lanes import DONE_JIRA_STATUSES, lane_for, recompute_ball


def _merge(base: dict, over: dict) -> dict:
    """Deep-merge OVER onto BASE (dicts recurse, everything else replaces)."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


# ── the 17-rule table, as (rule#, name, cached-fragment, kwargs, verdict) ──────
# Each fragment fires EXACTLY its own rule when alone (the individual tests
# prove that), and the fragments are built so any two compatible ones can be
# merged without a third rule firing in between.

RULES: tuple[tuple[int, str, dict, dict, tuple], ...] = (
    (1, "terminal_jira", {"jira": {"status": "Done"}}, {}, ("none", "Done")),
    (2, "slack_done", {"slack": {"done": True}}, {}, ("none", "done")),
    (3, "manual_closed", {"manual": {"open": False}}, {}, ("none", "done")),
    (4, "sig_done",
     {"ext": {"signal": {"state": "done", "detail": "shipped"}}},
     {"origin": "ext"}, ("none", "shipped")),
    (5, "agent_running", {"agent": {"running": True}}, {}, ("ai", "claude · working")),
    (6, "claim_working",
     {"claims": {"plug": {"state": "working", "detail": "plug busy"}}},
     {}, ("ai", "plug busy")),
    (7, "agent_waiting", {"agent": {"waiting": True}}, {}, ("human", "needs your input")),
    (8, "hold_label", {"jira": {"labels": ["conductor-hold"]}}, {}, ("human", "awaiting input")),
    (9, "manual_note", {"manual": {"open": True}}, {}, ("human", "note")),
    (10, "review_requested",
     {"github": {"review_requested_me": True, "reviewed_by_me": False, "state": "open"}},
     {}, ("human", "review requested")),
    (11, "sig_working",
     {"ext": {"signal": {"state": "working", "detail": "syncing"}}},
     {"origin": "ext"}, ("ai", "syncing")),
    (12, "open_pr", {"github": {"state": "open", "number": 7, "ci": "failing"}},
     {}, ("human", "ci failing")),
    (13, "assignee_me", {"jira": {"assignee_me": True, "status": "In Progress"}},
     {}, ("human", "In Progress")),
    (14, "slack_unread", {"slack": {"unread": True, "kind": "dm"}}, {}, ("human", "slack dm")),
    (15, "sig_needs_me",
     {"ext": {"signal": {"state": "needs_me", "detail": "ping"}}},
     {"origin": "ext"}, ("human", "ping")),
    (16, "enrich_signal",
     {"signals": {"sentry": {"state": "needs_me", "detail": "error spike"}}},
     {}, ("human", "error spike")),
    # 17 = the default; it cannot "fire alongside" anything, pinned separately.
)

#: Pairs whose firing conditions are mutually exclusive on one cached dict
#: (same field, contradictory values) — merging would silently disarm the
#: lower rule, so the pair proves nothing and is excluded instead.
_INCOMPATIBLE: frozenset[tuple[int, int]] = frozenset({
    (3, 9),             # manual.open False vs True
    (4, 11), (4, 15), (11, 15),  # one own-origin signal has ONE state
})


# ── every rule fires when alone ────────────────────────────────────────────────

@pytest.mark.parametrize(
    "name,fragment,kwargs,expected",
    [(name, frag, kw, exp) for _, name, frag, kw, exp in RULES],
    ids=[f"r{num:02d}-{name}" for num, name, *_ in RULES],
)
def test_rule_fires_alone(name, fragment, kwargs, expected):
    assert recompute_ball(dict(fragment), **kwargs) == expected


# ── rule 1: terminal states ────────────────────────────────────────────────────

@pytest.mark.parametrize("status", sorted(DONE_JIRA_STATUSES))
def test_every_done_jira_status_is_terminal(status):
    assert recompute_ball({"jira": {"status": status}}) == ("none", status)


def test_done_jira_statuses_set_is_exactly_pinned():
    assert DONE_JIRA_STATUSES == {
        "Done", "Closed", "Resolved", "Cancelled", "Canceled",
        "Won't Do", "Released", "Merged",
    }


@pytest.mark.parametrize("state", ["merged", "closed"])
def test_gh_terminal_state(state):
    assert recompute_ball({"github": {"state": state}}) == ("none", state)


def test_gh_terminal_detail_prefers_jira_status():
    c = {"jira": {"status": "Done"}, "github": {"state": "merged"}}
    assert recompute_ball(c) == ("none", "Done")


def test_non_done_jira_status_is_not_terminal():
    c = {"jira": {"status": "In Progress", "assignee_me": True}}
    assert recompute_ball(c) == ("human", "In Progress")  # rule 15, not rule 1


# ── rule 3: only an EXPLICIT open=False closes ─────────────────────────────────

def test_missing_manual_key_is_not_closed():
    # .get() → None is not `is False`; card falls through to the default
    assert recompute_ball({"manual": {}}) == ("none", None)


# ── rule 4/13/17: own-origin signal only ───────────────────────────────────────

def test_signal_read_only_from_own_origin():
    c = {"other": {"signal": {"state": "done", "detail": "shipped"}},
         "jira": {"assignee_me": True}}
    # a FOREIGN namespace's signal must not close the card
    assert recompute_ball(c, origin="ext") == ("human", "assigned")


def test_signal_ignored_without_origin():
    c = {"ext": {"signal": {"state": "done", "detail": "shipped"}},
         "jira": {"assignee_me": True}}
    assert recompute_ball(c) == ("human", "assigned")


@pytest.mark.parametrize("state,expected", [
    ("done", ("none", "done")),
    ("working", ("ai", "working")),
    ("needs_me", ("human", "needs you")),
], ids=["done", "working", "needs_me"])
def test_signal_detail_defaults(state, expected):
    c = {"ext": {"signal": {"state": state}}}
    assert recompute_ball(c, origin="ext") == expected


# ── rule 5: agent running ──────────────────────────────────────────────────────

def test_agent_running_bg_shows_watch_target():
    c = {"agent": {"running": True, "bg": "ci-watch"}}
    assert recompute_ball(c) == ("ai", "⏵ ci-watch")


# ── rule 6: claims — first-truthy-value selection ──────────────────────────────

def test_claim_detail_defaults_to_working():
    c = {"claims": {"plug": {"state": "working"}}}
    assert recompute_ball(c) == ("ai", "working")


def test_claim_selection_skips_falsy_and_malformed_entries():
    c = {"claims": {
        "a": {"state": None},
        "b": "junk",
        "c": {"state": "working", "detail": "c works"},
    }}
    assert recompute_ball(c) == ("ai", "c works")


def test_claim_selection_is_first_truthy_not_first_working():
    # the FIRST truthy-state claim is elected even when a later one says
    # "working" — rule 6 then does not fire at all. Pinned current behavior.
    c = {"claims": {
        "a": {"state": "paused"},
        "b": {"state": "working", "detail": "b works"},
    }}
    assert recompute_ball(c) == ("none", None)


def test_all_falsy_claims_fall_through():
    c = {"claims": {"a": {"state": ""}}, "agent": {"waiting": True}}
    assert recompute_ball(c) == ("human", "needs your input")


# ── rule 6 vs 7: the owner's explicit precedence ruling ────────────────────────

def test_foreign_claim_outranks_agent_waiting():
    """Owner ruling (survived a CodeRabbit challenge): a plugin's working
    claim beats a waiting dashboard claude — something IS moving."""
    c = {"claims": {"plug": {"state": "working", "detail": "plug busy"}},
         "agent": {"waiting": True}}
    assert recompute_ball(c) == ("ai", "plug busy")


# ── rule 8: hold label parameter ───────────────────────────────────────────────

def test_custom_hold_label_honored():
    c = {"jira": {"labels": ["my-hold"]}}
    assert recompute_ball(c, hold_label="my-hold") == ("human", "awaiting input")


def test_default_hold_label_ignores_other_labels():
    assert recompute_ball({"jira": {"labels": ["my-hold"]}}) == ("none", None)


# ── rule 10: review-request gating ─────────────────────────────────────────────

def test_already_reviewed_does_not_ask_again():
    c = {"github": {"review_requested_me": True, "reviewed_by_me": True, "state": "open"}}
    assert recompute_ball(c) == ("none", None)


def test_review_request_defaults_missing_state_to_open():
    c = {"github": {"review_requested_me": True}}
    assert recompute_ball(c) == ("human", "review requested")


# ── rule 12: open PR ci wording ────────────────────────────────────────────────

@pytest.mark.parametrize("ci,label", [
    ("failing", "ci failing"),
    ("passing", "ready to merge"),
    (None, "review / merge"),
    ("pending", "review / merge"),
], ids=["failing", "passing", "none", "pending"])
def test_open_pr_ci_labels(ci, label):
    c = {"github": {"state": "open", "number": 7, "ci": ci}}
    assert recompute_ball(c) == ("human", label)


def test_open_pr_without_number_is_not_mine():
    assert recompute_ball({"github": {"state": "open"}}) == ("none", None)


# ── rules 13/14: detail fallbacks ──────────────────────────────────────────────

def test_assignee_without_status_reads_assigned():
    assert recompute_ball({"jira": {"assignee_me": True}}) == ("human", "assigned")


def test_slack_unread_without_kind():
    assert recompute_ball({"slack": {"unread": True}}) == ("human", "slack")


# ── rule 16: promote-only enrichment signals ───────────────────────────────────

def test_enrich_contributors_arbitrate_by_name():
    c = {"signals": {
        "zeta": {"state": "needs_me", "detail": "z first?"},
        "alpha": {"state": "needs_me", "detail": "a first"},
    }}
    assert recompute_ball(c) == ("human", "a first")


def test_enrich_detail_default_names_contributor():
    c = {"signals": {"sentry": {"state": "needs_me"}}}
    assert recompute_ball(c) == ("human", "sentry: needs you")


def test_enrich_promote_only_states_ignored():
    c = {"signals": {"a": {"state": "done"}, "b": {"state": "working"}, "c": "junk"}}
    assert recompute_ball(c) == ("none", None)


# ── rule 17: the default ───────────────────────────────────────────────────────

def test_default_empty_card():
    assert recompute_ball({}) == ("none", None)


def test_default_reports_jira_status():
    assert recompute_ball({"jira": {"status": "Blocked"}}) == ("none", "Blocked")


def test_default_falls_back_to_sig_detail():
    c = {"ext": {"signal": {"state": "someday", "detail": "stalled"}}}
    assert recompute_ball(c, origin="ext") == ("none", "stalled")


# ── pairwise precedence: rule i beats every lower rule j ───────────────────────

_PAIRS = [
    (hi, lo)
    for i, hi in enumerate(RULES)
    for lo in RULES[i + 1:]
    if (hi[0], lo[0]) not in _INCOMPATIBLE
]


@pytest.mark.parametrize(
    "hi,lo", _PAIRS,
    ids=[f"r{hi[0]:02d}-{hi[1]}-beats-r{lo[0]:02d}-{lo[1]}" for hi, lo in _PAIRS],
)
def test_higher_rule_wins_when_both_fire(hi, lo):
    _, _, hi_frag, hi_kwargs, hi_expected = hi
    _, _, lo_frag, lo_kwargs, _ = lo
    cached = _merge(_merge({}, lo_frag), hi_frag)  # higher rule wins field conflicts
    kwargs = {**lo_kwargs, **hi_kwargs}
    assert recompute_ball(cached, **kwargs) == hi_expected


# ── lane_for(ball) mapping ─────────────────────────────────────────────────────

@pytest.mark.parametrize("ball,lane", [
    ("human", "need_human"),
    ("ai", "ai_working"),
    ("none", "done"),
    ("garbage", "done"),  # unknown ball degrades to done
], ids=["human", "ai", "none", "unknown-default"])
def test_lane_for(ball, lane):
    assert lane_for(ball) == lane
