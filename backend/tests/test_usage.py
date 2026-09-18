"""/api/usage: the statusline snapshot, with rolled-over windows reported as unknown.

The snapshot in ~/.claude/rate-limits.json is push-based — it only changes when a Claude
Code session renders its statusline. After a window resets, nothing rewrites the file
until the next render, so the pre-reset percentage sits there looking authoritative (the
header once showed "WEEK 88%" well after that week had reset). A resets_at in the past
proves the number is obsolete, so the endpoint nulls it and flags `expired`.
"""

from __future__ import annotations

import json
import time

import pytest

from conductor.api import usage


def _write(tmp_path, five_hour: dict | None, seven_day: dict | None, captured_at=123):
    rl = {}
    if five_hour is not None:
        rl["five_hour"] = five_hour
    if seven_day is not None:
        rl["seven_day"] = seven_day
    p = tmp_path / "rate-limits.json"
    p.write_text(json.dumps({"captured_at": captured_at, "rate_limits": rl}))
    return p


@pytest.fixture
def rl_file(tmp_path, monkeypatch):
    def _set(**kw):
        p = _write(tmp_path, kw.get("five_hour"), kw.get("seven_day"), kw.get("captured_at", 123))
        monkeypatch.setattr(usage, "_RL", p)
        return p

    return _set


async def test_live_windows_pass_through(rl_file):
    soon = time.time() + 3600
    rl_file(five_hour={"used_percentage": 3, "resets_at": soon},
            seven_day={"used_percentage": 88, "resets_at": soon + 86400})
    out = await usage.usage()
    assert out["session"] == {"used_percentage": 3, "resets_at": soon, "expired": False}
    assert out["weekly"]["used_percentage"] == 88
    assert out["captured_at"] == 123


async def test_expired_window_reports_unknown_not_the_stale_percentage(rl_file):
    """The reported bug: the week rolled over, no session had re-reported yet, and the
    header kept showing the pre-reset 88%."""
    past = time.time() - 60
    rl_file(five_hour={"used_percentage": 3, "resets_at": time.time() + 3600},
            seven_day={"used_percentage": 88, "resets_at": past})
    out = await usage.usage()
    assert out["weekly"]["used_percentage"] is None  # not 88
    assert out["weekly"]["expired"] is True
    assert out["weekly"]["resets_at"] == past  # kept: the UI still says when it rolled
    assert out["session"]["used_percentage"] == 3  # unaffected window untouched


async def test_reset_exactly_now_counts_as_expired(rl_file):
    rl_file(seven_day={"used_percentage": 50, "resets_at": time.time()})
    out = await usage.usage()
    assert out["weekly"]["expired"] is True


async def test_missing_resets_at_is_not_expired(rl_file):
    """No reset time means no evidence of staleness — don't invent it."""
    rl_file(seven_day={"used_percentage": 50})
    out = await usage.usage()
    assert out["weekly"] == {"used_percentage": 50, "resets_at": None, "expired": False}


async def test_boolean_resets_at_is_not_a_timestamp(rl_file):
    """bool is an int subclass; `True <= now` would silently expire a window."""
    rl_file(seven_day={"used_percentage": 50, "resets_at": True})
    out = await usage.usage()
    assert out["weekly"]["expired"] is False
    assert out["weekly"]["used_percentage"] == 50


async def test_absent_window_is_null(rl_file):
    rl_file(five_hour={"used_percentage": 1, "resets_at": time.time() + 60})
    out = await usage.usage()
    assert out["weekly"] is None


async def test_missing_or_malformed_file_is_all_null(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "_RL", tmp_path / "nope.json")
    assert await usage.usage() == {"session": None, "weekly": None, "captured_at": None}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    monkeypatch.setattr(usage, "_RL", bad)
    assert await usage.usage() == {"session": None, "weekly": None, "captured_at": None}
