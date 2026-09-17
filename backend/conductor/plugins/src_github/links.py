"""This source's half of free-text link discovery (M8 split of core's old
``links.discover_links``): GitHub PR references, either a full URL or the
``owner/repo#123`` shorthand — a URL match wins over a shorthand match for the
same ref (byte-identical to the pre-split function's setdefault precedence).
See ``conductor.links`` for the collection point and
``conductor.plugins.base.LinkMatcherSpec`` for the contract."""

from __future__ import annotations

import re

_PR_URL = re.compile(r"https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)")
_PR_SHORT = re.compile(r"\b([\w.-]+/[\w.-]+)#(\d+)\b")


def match_pr_links(text: str, jira_base_url: str) -> list[dict]:
    out: dict[str, dict] = {}
    for m in _PR_URL.finditer(text):
        ref = f"{m.group(1)}#{m.group(2)}"
        out[ref] = {"kind": "pr", "ref": ref, "url": m.group(0), "title": ref, "auto": True}
    for m in _PR_SHORT.finditer(text):
        repo, num = m.group(1), m.group(2)
        ref = f"{repo}#{num}"
        out.setdefault(
            ref,
            {
                "kind": "pr",
                "ref": ref,
                "url": f"https://github.com/{repo}/pull/{num}",
                "title": ref,
                "auto": True,
            },
        )
    return list(out.values())
