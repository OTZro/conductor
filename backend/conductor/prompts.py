"""User-configurable Claude prompts.

Every prompt Conductor sends to `claude` (briefs, session seeds, handover) lives here
as an English default, overridable per-machine via a LOCAL JSON file at
`~/.conductor/prompts.json` (path via CONDUCTOR_PROMPTS_FILE) — same pattern as
dashboards.json / termkeys.json: the repo ships the defaults, each user keeps their
own language/phrasing outside version control.

The file maps prompt keys to template strings; unknown keys are warned + ignored,
missing keys fall back to the default. Templates use str.format placeholders — keep
the SAME placeholders as the default ({who}, {title}, {cid}, …); literal braces must be
doubled ({{ }}). `{base}` embeds the shipped default's text (so an override can wrap or
extend the default instead of re-writing it). The file is re-read on change (mtime), so
edits apply without a backend restart; a file that turns unreadable/corrupt KEEPS its
last-known-good content (warned once) rather than silently reverting to the defaults. A
template that fails to format falls back to the default rather than erroring the feature.

A ready-made Traditional-Chinese set ships in the repo:
`cp config/prompts.zh-TW.example.json ~/.conductor/prompts.json`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

# Appended IN CODE to every one-shot brief prompt (not user-overridable): the brief
# claude once tried to `acli`-lookup a ticket key it saw in a Slack message, got the
# print-mode permission denial, and emitted "please authorize acli…" AS the brief —
# which then stuck to the card (briefs are cached once). Belt: --disallowedTools at the
# call sites; suspenders: this instruction.
NO_TOOLS_GUARD = (
    "\n\n(Answer directly from the text above only — do not use tools, do not look "
    "anything up, and do not ask for permissions. If context is missing, summarize "
    "what is there.)"
)

DEFAULTS: dict[str, str] = {
    # -- slack brief (sources/slack.py): one-line "what is being asked of me".
    #    {who} = " (Display Name)" or "". The message text is appended after the template.
    "slack_brief_thread": (
        "Below is a Slack discussion (in time order; it ends with someone @-mentioning me{who} "
        "or a usergroup I belong to). Taking the whole thread into account, "
        "state in one sentence (English) what they need me to do or decide. "
        "Get straight to the point — no preamble, no pleasantries:"
    ),
    "slack_brief_single": (
        "Below is a Slack message that @-mentions me{who} or a usergroup I belong to "
        "(@channel/@develop and the like). "
        "State in one sentence (English) what they need me to do or decide. "
        "Get straight to the point — no preamble, no pleasantries:"
    ),
    # -- PR brief (sources/github.py): {title} {body} {review} are filled from the PR.
    "pr_brief": (
        "Below is a GitHub PR description, plus my latest review of it (usually posted "
        "on my behalf by an automated review agent). Summarize in English as two bullet "
        "points — straight to the point, concise, no preamble or pleasantries:\n"
        "1. What this PR does (1-2 sentences)\n"
        "2. My review conclusion: state the verdict first (APPROVE / REQUEST_CHANGES / "
        "COMMENT), then one sentence of reasoning. If I haven't reviewed yet, write "
        '"not reviewed yet"\n\n'
        "## PR: {title}\n{body}\n\n## My review:\n{review}"
    ),
    # -- seed for a fresh claude on a slack card (api/terminals.py); assembled in order.
    "seed_slack_mention": "I was mentioned on Slack:\n{text}",
    "seed_slack_link": "Slack link: {url}",
    "seed_slack_gist": "(gist: {brief})",
    "seed_slack_closing": "Please handle this for me; ask me first about anything unclear.",
    # -- handover between machines (api/terminals.py); {cid} = card id.
    "handover_write": (
        "Write a handover doc for the current task to ~/.conductor/handover/{cid}.md, covering: "
        "goal/why; current state (what's done, what's in progress); where the code is (branch + "
        "the latest few commits, and whether anything is unpushed); next steps (ordered list); "
        "key files; decisions already made; pitfalls hit; how to verify. When it's written, "
        "git commit and push the needed changes, then tell me the handover is ready."
    ),
    "handover_pickup": (
        "You are taking over a task from another machine. First read ~/.conductor/handover/{cid}.md "
        "for the full context and next steps (it names the branch); per the doc, git pull / checkout "
        "the right branch and bring it up to date, then continue the work. "
        "Ask me first about anything unclear."
    ),
}


class _Layer:
    """One prompts file, cached by mtime. A file that turns corrupt/unreadable keeps
    serving its LAST-KNOWN-GOOD content (a half-saved edit or bad JSON must not
    silently swap the user's prompts back to the shipped defaults); deleting the file
    is the deliberate way to drop the layer. Unknown keys are warned once per change."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._mtime: float | None = None
        self._bad_mtime: float | None = None  # last mtime that failed to parse (warn once)
        self._good: dict[str, str] = {}

    def read(self, path: Path | None) -> dict[str, str]:
        if path is None:
            return {}
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:  # gone on purpose → layer off
            self._mtime, self._good = None, {}
            return {}
        except OSError:  # transient stat error → last-known-good
            return self._good
        if mtime == self._mtime:
            return self._good
        try:
            raw = json.loads(path.read_text())
            if not isinstance(raw, dict):
                raise ValueError("top level must be an object")
        except (OSError, ValueError) as e:
            if mtime != self._bad_mtime:
                self._bad_mtime = mtime
                log.warning(
                    "prompts file %s (%s) unreadable (%s) — keeping the last good version",
                    path, self.name, e,
                )
            return self._good  # ← last-known-good, NOT the defaults
        for k in raw:
            if k not in DEFAULTS:
                log.warning("prompts file %s (%s): unknown key %r — ignored (typo?)", path, self.name, k)
        self._mtime, self._bad_mtime = mtime, None
        self._good = {k: v for k, v in raw.items() if k in DEFAULTS and isinstance(v, str)}
        return self._good


_local = _Layer("local")


def prompt(key: str, **fmt: object) -> str:
    """The prompt for `key`: the user's override if present, else the shipped default.
    `{base}` in an override expands to the default's text (placeholders fill in the same
    final pass). A template with broken placeholders falls back to the shipped default
    (a bad prompts.json must not take the feature down)."""
    default = DEFAULTS[key]
    resolved = _local.read(settings.prompts_file).get(key, default)
    if "{base}" in resolved:
        resolved = resolved.replace("{base}", default)
    try:
        return resolved.format(**fmt)
    except (KeyError, IndexError, ValueError) as e:
        if resolved is not default:
            log.warning("prompt %r override failed to format (%s) — using the default", key, e)
        return default.format(**fmt)
