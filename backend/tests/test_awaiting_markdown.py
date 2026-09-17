"""The awaiting-input panel must show a rendered workpad, not run-on text.

An external automation tool's workpad comment is ADF (headings/bold/code). `acli comment list` flattens
it to plain text — bold/code become bare double-spaces, newlines vanish — so the fix
fetches the comment's ADF via `workitem view` and converts it with the same
_adf_to_markdown the Jira description uses. These tests pin that pipeline.
"""

from __future__ import annotations

import json

import pytest

from conductor.actions import resume
from conductor.plugins.src_jira import jira


def _adf_workpad():
    """A miniature workpad ADF comment body."""
    return {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Workpad"}]},
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "text": "Phase: Building — parked at the ", "marks": [{"type": "strong"}]},
                    {"type": "text", "text": "AES_US_STATE_OPTIONS", "marks": [{"type": "code"}]},
                    {"type": "text", "text": " gate."},
                ],
            },
            {"type": "heading", "attrs": {"level": 3}, "content": [{"type": "text", "text": "Awaiting Input"}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Confirm Q1 — Guam scope. (A) drop Guam from scope (B) keep Guam in scope"}]},
        ],
    }


@pytest.mark.asyncio
async def test_fetch_uses_view_not_flattening_list(monkeypatch):
    """The fetch must go through `workitem view` (ADF), never `comment list` (flattened)."""
    seen: dict = {}

    async def fake_acli(args, timeout=30):
        seen["args"] = args
        return json.dumps({"fields": {"comment": {"comments": [{"id": "9", "created": "2026-08-03T19:10:00.000+0800", "body": _adf_workpad()}]}}})

    monkeypatch.setattr(jira, "_run_acli", fake_acli)
    await resume.fetch_comments("PROJ-4420")
    assert "view" in seen["args"] and "comment" in seen["args"]
    assert "list" not in seen["args"]  # the flattening endpoint


@pytest.mark.asyncio
async def test_fetch_and_parse_end_to_end(monkeypatch):
    view = {
        "fields": {
            "comment": {
                "comments": [
                    {"id": "9", "created": "2026-08-03T19:10:00.000+0800", "body": _adf_workpad()},
                    {"id": "1", "created": "2026-08-01T10:00:00.000+0800", "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "older note"}]}]}},
                ]
            }
        }
    }

    async def fake_acli(args, timeout=30):
        return json.dumps(view)

    monkeypatch.setattr(jira, "_run_acli", fake_acli)
    comments = await resume.fetch_comments("PROJ-4420")

    # oldest→newest, and the ADF became real markdown (bold + code survived)
    assert [c["id"] for c in comments] == ["1", "9"]
    workpad = comments[-1]["body"]
    assert "**Phase: Building" in workpad
    assert "`AES_US_STATE_OPTIONS`" in workpad
    assert "## Workpad" in workpad

    parsed = resume.parse_awaiting(comments)
    assert parsed is not None
    assert parsed["comment_id"] == "9"
    # the question keeps its markdown (the frontend renders it)
    assert "Guam" in parsed["question"]
    # enumerated options are still extracted for the answer buttons
    assert parsed["options"] == ["drop Guam from scope", "keep Guam in scope"]


@pytest.mark.asyncio
async def test_fetch_sorts_by_instant_not_string(monkeypatch):
    """Ordering is by the true instant, not lexicographic: +0800 19:10 (11:10 UTC)
    precedes +0000 12:00 even though its string sorts later."""
    view = {"fields": {"comment": {"comments": [
        {"id": "later", "created": "2026-08-03T12:00:00.000+0000", "body": None},   # 12:00 UTC
        {"id": "earlier", "created": "2026-08-03T19:10:00.000+0800", "body": None},  # 11:10 UTC
    ]}}}

    async def fake_acli(args, timeout=30):
        return json.dumps(view)

    monkeypatch.setattr(jira, "_run_acli", fake_acli)
    comments = await resume.fetch_comments("PROJ-1")
    assert [c["id"] for c in comments] == ["earlier", "later"]


def test_long_workpad_keeps_the_trailing_ask():
    """A real workpad puts its actionable "Next action" ask at the very end, past
    the old 1500/4000 cap. The question must include that tail, not truncate it."""
    filler = "Root cause analysis. " * 400  # ~8000 chars of context before the ask
    ask = "### ⏭️ Next action\n\n**Do this:** run the BAT steps and comment the result."
    comments = [{
        "id": "7", "created": "2026-08-05T10:00:00.000+0800",
        "body": f"## Workpad\n\n### Awaiting Input\n\n{filler}\n\n{ask}",
    }]
    parsed = resume.parse_awaiting(comments)
    assert parsed is not None
    assert len(parsed["question"]) > 4000  # not clipped to the old cap
    assert "⏭️ Next action" in parsed["question"]
    assert "run the BAT steps and comment the result" in parsed["question"]


def test_numbered_bat_steps_are_not_options():
    """A workpad's numbered BAT steps sit above the 'Next action' ask; they are
    instructions, not answer choices, and must not become answer buttons."""
    body = (
        "## Workpad\n\n### Awaiting Input\n\n### BAT\n\n"
        "1. Review the PR and confirm the diff.\n"
        "2. Run the suite locally with docker compose.\n"
        "3. Post-deploy, watch ELK for the rebuild event.\n\n"
        "### ⏭️ Next action\n\n"
        "**Do this:** run the BAT steps above and comment the result."
    )
    parsed = resume.parse_awaiting([{"id": "1", "created": "x", "body": body}])
    assert parsed is not None
    assert parsed["options"] == []  # scoped to the ask region, so the steps don't leak


def test_real_choices_in_the_ask_region_still_parse():
    """Genuine choices offered in the ask section are still surfaced as buttons."""
    body = (
        "## Workpad\n\n### Awaiting Input\n\n(1. throwaway 2. context in the analysis)\n\n"
        "### ⏭️ Next action\n\nPick one: (A) ship as-is (B) revert the change"
    )
    parsed = resume.parse_awaiting([{"id": "1", "created": "x", "body": body}])
    assert parsed is not None
    assert parsed["options"] == ["ship as-is", "revert the change"]


def test_parse_awaiting_none_without_workpad():
    comments = [{"id": "1", "created": "x", "body": "just a normal comment, no markers"}]
    assert resume.parse_awaiting(comments) is None


@pytest.mark.asyncio
async def test_fetch_legacy_string_body_is_escaped(monkeypatch):
    """A legacy (non-ADF) string body is Jira wiki markup — escape it like
    fetch_description so it renders as typed, not as markdown."""
    view = {"fields": {"comment": {"comments": [
        {"id": "1", "created": "2026-08-01T10:00:00.000+0800", "body": "see *notes* here"},
        {"id": "2", "created": "2026-08-02T10:00:00.000+0800", "body": None},
    ]}}}

    async def fake_acli(args, timeout=30):
        return json.dumps(view)

    monkeypatch.setattr(jira, "_run_acli", fake_acli)
    comments = await resume.fetch_comments("PROJ-1")
    assert comments[0]["body"] == r"see \*notes\* here"  # the * won't open emphasis
    assert comments[1]["body"] == ""  # None body → empty string, no crash


@pytest.mark.asyncio
async def test_fetch_handles_empty_and_flat_shapes(monkeypatch):
    """No comments, and the top-level (no "fields" wrapper) acli shape, both degrade
    to [] instead of raising."""
    shapes = [
        {"fields": {"comment": {"comments": []}}},  # ticket with zero comments
        {"fields": {}},                              # no comment field at all
        {"comment": {"comments": []}},               # flat shape (no "fields" wrapper)
    ]
    for shape in shapes:
        async def fake_acli(args, timeout=30, _s=shape):
            return json.dumps(_s)

        monkeypatch.setattr(jira, "_run_acli", fake_acli)
        assert await resume.fetch_comments("PROJ-1") == []


@pytest.mark.parametrize(
    "body",
    [
        "(A) drop Guam (B) keep Guam",          # plain
        "**(A)** drop Guam **(B)** keep Guam",  # bold markers (ADF strong)
        r"\(A\) drop Guam \(B\) keep Guam",     # backslash-escaped parens
        "1. drop Guam\n2. keep Guam",           # ADF ordered list
    ],
)
def test_options_survive_markdown_formatting(body):
    """Option markers must still parse once the body is markdown — bold/escaped/
    list forms all arrive from the ADF converter, and the answer buttons depend on
    them (regression guard for the markdown fetch)."""
    assert resume._extract_options(body) == ["drop Guam", "keep Guam"]


def test_parse_awaiting_workpad_without_options():
    """A workpad question with no enumerated options still parses (options == [])
    so the answer-button UI just shows the free-form box."""
    comments = [{
        "id": "5", "created": "2026-08-03T10:00:00.000+0800",
        "body": "## Workpad\n\n## Awaiting Input\n\nPlease confirm the plan and reply.",
    }]
    parsed = resume.parse_awaiting(comments)
    assert parsed is not None
    assert parsed["comment_id"] == "5"
    assert parsed["options"] == []
