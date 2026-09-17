"""Regression tests for the github source's pure logic.

Each case here is a shipped production bug:
- PR #831 showed "Approved" when an automated review tool had only COMMENTED a
  suggested verdict (_review_status must not read a suggestion as a formal approval).
- PROJ-727's PR search once matched PROJ-7271 (_key_pattern substring bleed).
"""

from __future__ import annotations

from conductor.plugins.src_github.impl import (
    _ci_checks,
    _ci_summary,
    _key_pattern,
    _latest_per_check,
    _my_latest_review,
    _review_status,
)

# ── _review_status ───────────────────────────────────────────────────────────


def _rev(state: str, body: str = "") -> dict:
    return {"state": state, "body": body, "submittedAt": "2026-07-01T00:00:00Z"}


def test_formal_approval_is_approved():
    assert _review_status(_rev("APPROVED")) == "APPROVED"


def test_changes_requested_is_blocking():
    assert _review_status(_rev("CHANGES_REQUESTED")) == "CHANGES_REQUESTED"


def test_suggested_approve_is_not_a_real_approval():
    # an automated review tool "approves" via a COMMENTED review whose body carries
    # the verdict — the PR #831 bug showed this as ✓ Approved. It must stay distinct.
    body = "🤖 **Suggested verdict: APPROVE**\n\nLooks good…"
    assert _review_status(_rev("COMMENTED", body)) == "SUGGEST_APPROVE"


def test_dismissed_suggested_approve_still_suggest():
    body = "🤖 **Suggested verdict: APPROVE**"
    assert _review_status(_rev("DISMISSED", body)) == "SUGGEST_APPROVE"


def test_plain_comment_is_commented():
    assert _review_status(_rev("COMMENTED", "just a note")) == "COMMENTED"


def test_no_review_is_none():
    assert _review_status(None) is None


# ── _my_latest_review ────────────────────────────────────────────────────────


def test_latest_review_filters_by_login_and_recency():
    reviews = [
        {"author": {"login": "devuser"}, "state": "COMMENTED", "submittedAt": "2026-07-01T01:00:00Z"},
        {"author": {"login": "averynexus"}, "state": "APPROVED", "submittedAt": "2026-07-01T09:00:00Z"},
        {"author": {"login": "devuser"}, "state": "APPROVED", "submittedAt": "2026-07-01T05:00:00Z"},
        {"author": {"login": "devuser"}, "state": "PENDING", "submittedAt": "2026-07-01T08:00:00Z"},
    ]
    latest = _my_latest_review(reviews, "devuser")
    # someone else's review (averynexus) is ignored; my PENDING draft is ignored;
    # my newest submitted review wins.
    assert latest["state"] == "APPROVED"
    assert latest["submittedAt"] == "2026-07-01T05:00:00Z"


def test_latest_review_without_login_is_none():
    assert _my_latest_review([_rev("APPROVED")], None) is None


# ── _key_pattern ─────────────────────────────────────────────────────────────


def test_key_pattern_is_exact_not_prefix():
    pat = _key_pattern("PROJ-727")
    assert pat.search("fix: thing [PROJ-727]")
    assert pat.search("feat/proj-727-branch")  # case-insensitive
    assert not pat.search("PROJ-7271 unrelated")


# ── _ci_summary ──────────────────────────────────────────────────────────────


def test_ci_summary_failure_dominates():
    rollup = [{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}, {"status": "IN_PROGRESS"}]
    assert _ci_summary(rollup) == "failing"


def test_ci_summary_pending_before_pass():
    assert _ci_summary([{"conclusion": "SUCCESS"}, {"status": "QUEUED"}]) == "pending"
    assert _ci_summary([{"conclusion": "SUCCESS"}]) == "passing"
    assert _ci_summary([]) == "none"


def test_rerun_supersedes_cancelled_run():
    """PROJ-8175: a check that was CANCELLED then re-ran green leaves BOTH entries in the
    rollup. Latest-per-name must keep the newer SUCCESS so a green PR isn't shown
    failing."""
    rollup = [
        {"name": "ci / test", "status": "COMPLETED", "conclusion": "CANCELLED",
         "completedAt": "2026-07-07T09:53:31Z"},
        {"name": "ci / test", "status": "COMPLETED", "conclusion": "SUCCESS",
         "completedAt": "2026-07-07T10:26:06Z"},
    ]
    deduped = _latest_per_check(rollup)
    assert len(deduped) == 1 and deduped[0]["conclusion"] == "SUCCESS"
    assert _ci_summary(deduped) == "passing"
    assert _ci_checks(deduped) == []  # nothing non-green to surface
