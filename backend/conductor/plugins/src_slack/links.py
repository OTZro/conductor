"""This source's half of free-text link discovery (M8 split of core's old
``links.discover_links``): Slack permalinks mentioned in free text. See
``conductor.links`` for the collection point and
``conductor.plugins.base.LinkMatcherSpec`` for the contract."""

from __future__ import annotations

import re

_SLACK_URL = re.compile(r"https?://[\w-]+\.slack\.com/archives/\S+")


def match_slack_links(text: str, jira_base_url: str) -> list[dict]:
    out: dict[str, dict] = {}
    for m in _SLACK_URL.finditer(text):
        url = m.group(0)
        out[url] = {"kind": "slack", "ref": url, "url": url, "title": "slack", "auto": True}
    return list(out.values())
