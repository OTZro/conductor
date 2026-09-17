"""Unit tests for the two pure cores: ball/lane derivation and link discovery."""

from conductor.lanes import lane_for, recompute_ball
from conductor.links import discover_links


# --- recompute_ball: precedence-ordered signal rules ---


def test_hold_label_is_need_human():
    ball, state = recompute_ball({"jira": {"labels": ["conductor-hold"], "status": "Committed"}})
    assert ball == "human"
    assert state == "awaiting input"
    assert lane_for(ball) == "need_human"


def test_review_requested_is_need_human():
    ball, _ = recompute_ball({"github": {"review_requested_me": True, "state": "open"}})
    assert ball == "human"
    assert lane_for(ball) == "need_human"


def test_running_is_ai_working():
    ball, state = recompute_ball({"claims": {"plugin": {"state": "working", "detail": "Building"}}})
    assert ball == "ai"
    assert state == "Building"
    assert lane_for(ball) == "ai_working"


def test_assigned_open_is_need_human():
    ball, _ = recompute_ball(
        {"jira": {"assignee_me": True, "status": "In Progress", "labels": []}}
    )
    assert ball == "human"


def test_done_status_is_done():
    ball, _ = recompute_ball({"jira": {"assignee_me": True, "status": "Done", "labels": []}})
    assert ball == "none"
    assert lane_for(ball) == "done"


def test_slack_done_resurfaces_when_done_cleared():
    """A new message in a Done slack thread clears slack.done (see slack._upsert); the
    card then returns to Need Human via the unread rule."""
    assert recompute_ball({"slack": {"done": True, "unread": True}})[0] == "none"
    assert recompute_ball({"slack": {"done": False, "unread": True}})[0] == "human"


def test_merged_pr_is_done_even_if_review_requested():
    ball, _ = recompute_ball({"github": {"state": "merged", "review_requested_me": True}})
    assert ball == "none"


def test_hold_precedes_running():
    # the hold label outranks a lower-priority "working" signal — parking the
    # ticket means the ball is yours even while something nominally reports busy.
    ball, _ = recompute_ball(
        {"jira": {"labels": ["conductor-hold"]}, "ext": {"signal": {"state": "working"}}},
        origin="ext",
    )
    assert ball == "human"


def test_done_beats_stale_hold_label():
    # a Done ticket with a lingering hold label should still be Done
    ball, _ = recompute_ball({"jira": {"status": "Done", "labels": ["conductor-hold"]}})
    assert ball == "none"
    assert lane_for(ball) == "done"


def test_empty_is_done():
    ball, _ = recompute_ball({})
    assert ball == "none"


# --- discover_links: cross-link ticket / PR / Slack out of free text ---


def test_link_discovery_finds_jira_and_pr_url():
    links = discover_links(
        "feat(PROJ-13493): add thing, see https://github.com/your-org/fms/pull/19804",
        "https://your-org.atlassian.net",
    )
    pairs = {(l["kind"], l["ref"]) for l in links}
    assert ("jira", "PROJ-13493") in pairs
    assert ("pr", "your-org/fms#19804") in pairs


def test_link_discovery_pr_shorthand():
    links = discover_links("landed in your-org/fms#19804", "https://x")
    assert any(l["kind"] == "pr" and l["ref"] == "your-org/fms#19804" for l in links)


def test_link_discovery_slack_permalink():
    url = "https://your-org.slack.com/archives/C079Z6JLF2P/p1700000000000000"
    links = discover_links(f"discussed here {url}", "https://x")
    assert any(l["kind"] == "slack" and l["ref"] == url for l in links)


def test_link_discovery_empty():
    assert discover_links(None, "https://x") == []
    assert discover_links("no refs here", "https://x") == []


# ── agent-state precedence (the PROJ-1320 class of bug) ───────────────────────


def test_done_beats_stale_agent_waiting():
    """PROJ-1320: a closed ticket sat in Need Human because a dead session's
    Stop-hook left agent.waiting=true — terminal states must win over agent flags."""
    ball, state = recompute_ball(
        {"jira": {"status": "Done", "assignee_me": True},
         "agent": {"active": True, "running": False, "waiting": True}}
    )
    assert ball == "none"
    assert state == "Done"


def test_manual_done_beats_stale_agent_waiting():
    """A manual note marked done (open=False) with a lingering dashboard-claude
    session (agent.waiting=true) was stuck in Need Human: the agent check
    intercepted it before it could settle, and ball never reached "none" so
    kill-on-done never reaped the session. Manual-done must win like slack.done."""
    ball, state = recompute_ball(
        {"manual": {"open": False},
         "agent": {"active": True, "running": False, "waiting": True}}
    )
    assert ball == "none"
    assert state == "done"


def test_open_manual_note_is_still_need_human():
    """Guard the other leg: an OPEN note (open=True) stays the user's — the
    terminal short-circuit must not swallow live notes."""
    ball, _ = recompute_ball({"manual": {"open": True}})
    assert ball == "human"


def test_merged_pr_beats_agent_running():
    ball, _ = recompute_ball(
        {"github": {"state": "merged", "number": 1},
         "agent": {"active": True, "running": True, "waiting": False}}
    )
    assert ball == "none"


def test_agent_running_is_ai_working():
    ball, state = recompute_ball(
        {"jira": {"status": "Building", "assignee_me": True},
         "agent": {"active": True, "running": True, "waiting": False}}
    )
    assert ball == "ai"
    assert "working" in state


def test_agent_bg_monitor_label_surfaces():
    """A claude that parked a background CI watcher is still AI-working, and the
    card should say what it's watching."""
    ball, state = recompute_ball(
        {"jira": {"status": "Building", "assignee_me": True},
         "agent": {"active": True, "running": True, "waiting": False,
                   "bg": "Watch PR 113 CI checks"}}
    )
    assert ball == "ai"
    assert state == "⏵ Watch PR 113 CI checks"


def test_agent_waiting_on_open_ticket_is_need_human():
    ball, state = recompute_ball(
        {"jira": {"status": "Building", "assignee_me": True},
         "agent": {"active": True, "running": False, "waiting": True}}
    )
    assert ball == "human"
    assert state == "needs your input"
