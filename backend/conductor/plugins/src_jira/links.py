"""This source's half of free-text link discovery (M8 split of core's old
``links.discover_links``): jira ticket keys mentioned in a card's description /
slack text / PR body etc. See ``conductor.links`` for the collection point and
``conductor.plugins.base.LinkMatcherSpec`` for the contract."""

from __future__ import annotations

import re

_JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")


def match_jira_links(text: str, jira_base_url: str) -> list[dict]:
    out: dict[str, dict] = {}
    for m in _JIRA_KEY.finditer(text):
        key = m.group(1)
        out[key] = {
            "kind": "jira",
            "ref": key,
            "url": f"{jira_base_url}/browse/{key}",
            "title": key,
            "auto": True,
        }
    return list(out.values())
