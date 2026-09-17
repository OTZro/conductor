from __future__ import annotations

import os
import re
import tempfile

from ..config import settings
from ..proc import run
from ..plugins.src_jira import jira as jira_src  # via the source plugin (M5)

# An external automation tool posts a "Workpad … Awaiting Input …" comment when it
# parks a ticket for a human. The resume contract: leave a comment newer than the
# workpad + remove the hold label. Keyed only on "awaiting input" (case-insensitive) —
# any tool that phrases a parked-ticket comment this way is recognized, not just one
# specific workpad convention.
_MARKERS = ("awaiting input",)

# comment fetch lives in the jira source (next to fetch_description — same
# acli-view → ADF → markdown pipeline); resume.py only owns the workpad parsing.
fetch_comments = jira_src.fetch_comments

# Boilerplate that follows the actual question — trim it from the displayed text.
_STOPS = (
    "To hand this back",
    "To hand rework back",
    "leave a comment with your choice",
)

_OPTION_PATTERNS = [
    re.compile(r"\(([A-Da-d])\)\s+([^()]{3,140}?)(?=\s*\([A-Da-d]\)|$)"),
    # one line per numbered option ([^.\n] so a match can't run past the item's own
    # line into the next — an ADF ordered list renders as "1. …\n2. …")
    re.compile(r"(?:^|\s)([1-6])[.)]\s+([^.\n]{3,140})"),
]

# the comment body is markdown now, so an option marker can arrive bold (**(A)**),
# code-wrapped, or backslash-escaped (\(A\), 1\.) — strip those before matching so
# the answer buttons survive the ADF→markdown conversion (else the (A)/1. regexes,
# which want the raw marker, silently find nothing).
_MD_ESCAPE = re.compile(r"\\([\\`*_~\[\]()#>+.!|-])")
_MD_EMPHASIS = re.compile(r"[*_~`]")


def _strip_inline_md(text: str) -> str:
    return _MD_EMPHASIS.sub("", _MD_ESCAPE.sub(r"\1", text))


def parse_awaiting(comments: list[dict]) -> dict | None:
    """Find the newest awaiting-input comment and extract the question + any
    best-effort enumerated options. Comments are ordered +created (ascending) so the
    last match is the newest."""
    workpad = None
    for c in comments:
        body = (c.get("body") or "").lower()
        if any(m in body for m in _MARKERS):
            workpad = c
    if not workpad:
        return None
    body = workpad.get("body") or ""
    return {
        "question": _extract_question(body),
        "options": _extract_options(body),
        "raw": body,
        "comment_id": str(workpad["id"]) if workpad.get("id") else None,
    }


def _extract_question(body: str) -> str:
    low = body.lower()
    idx = low.rfind("awaiting input")
    seg = body[idx + len("awaiting input") :].strip() if idx >= 0 else body.strip()
    # the hold-label phrasing is read from settings, not hardcoded, so a renamed
    # label still gets trimmed as boilerplate rather than leaking into the question.
    stops = (*_STOPS, f"remove the {settings.jira_hold_label}")
    for stop in stops:
        j = seg.find(stop)
        if j > 0:
            seg = seg[:j].strip()
            break
    # a real workpad is a full document (root cause → BAT steps → the "⏭️ Next
    # action" ask at the very end); the actionable ask lives at the bottom, so a
    # tight cap silently hides exactly what the human must respond to. Show it
    # whole — the panel scrolls — and keep only a runaway guard against a
    # pathologically huge comment (a mid-markdown cut just strands a delimiter).
    return (seg or body).strip()[:_MAX_QUESTION]


_MAX_QUESTION = 40000  # ~10k tokens: no real workpad hits this; guards a giant paste


# Real answer choices live in the ask itself (the "Next action" / "Awaiting Input"
# section at the end), never in the analysis above it. Scope option-matching there so
# a workpad's numbered BAT steps ("1. Run the suite…") can't masquerade as choices.
_ASK_ANCHORS = ("next action", "awaiting input")


def _ask_region(body: str) -> str:
    idx = max((body.lower().rfind(a) for a in _ASK_ANCHORS), default=-1)
    return body[idx:] if idx >= 0 else body


def _extract_options(body: str) -> list[str]:
    body = _strip_inline_md(_ask_region(body))
    for pat in _OPTION_PATTERNS:
        opts: list[str] = []
        for m in pat.finditer(body):
            text = m.group(2).strip(" .")
            if text and text not in opts:
                opts.append(text)
        if len(opts) >= 2:
            return opts[:6]
    return []


async def post_answer_and_release(key: str, answer: str) -> dict:
    """The hero action: post the human's answer as a NEW comment (newer than the
    workpad, satisfying the automation tool's resume guard) then remove the hold
    label so the tool re-picks the ticket."""
    body = f"{answer}\n\n— via Conductor"
    fd, path = tempfile.mkstemp(suffix=".txt", text=True)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        await run(
            settings.acli_bin,
            "jira", "workitem", "comment", "create", "--key", key, "--body-file", path,
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    await run(
        settings.acli_bin,
        "jira", "workitem", "edit", key, "--remove-labels", settings.jira_hold_label,
    )
    return {"ok": True}
