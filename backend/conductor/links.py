"""Free-text link discovery — the seam behind ``store.upsert_card``'s ``link_text``.

M8: split out of one hardcoded jira+github+slack regex function into a plugin
extension point. Each source plugin owning a link kind (src_jira / src_github /
src_slack) declares a :class:`~conductor.plugins.base.LinkMatcherSpec` — its own
regex/matcher over free text — instead of this module importing those plugins
directly (core stays source-agnostic; per M8's constraint the source plugins
also never import each other). This module is the genuinely source-neutral
remainder: the dedup/merge across every registered matcher's results, same role
``enrich.py`` plays for link-kind hover enrichment."""

from __future__ import annotations

import logging

log = logging.getLogger("conductor.links")


def discover_links(text: str | None, jira_base_url: str) -> list[dict]:
    """Extract related ticket / PR / Slack links from free text via every
    registered :class:`LinkMatcherSpec`. Deduped by (kind, ref) — kinds never
    collide across matchers (each source owns its own kind), so this reduces to
    first-registrant-wins per key, matching the pre-split function's dict-
    insertion-order behavior byte for byte. Imported lazily (plugins import
    this module's consumer, ``store.py``)."""
    if not text:
        return []
    from .plugins import runtime

    out: dict[tuple[str, str], dict] = {}
    for plugin_id, spec in runtime.spec_rows("link_matchers"):
        try:
            found = spec.match(text, jira_base_url) or ()
            for link in found:
                out.setdefault((link["kind"], link["ref"]), link)
        except Exception as exc:  # noqa: BLE001 — a broken matcher (incl. a malformed
            # result missing kind/ref) must not break the upsert path for every OTHER
            # source; isolate the WHOLE matcher, match() call and result consumption alike
            log.warning("[links] matcher for %r failed: %s", plugin_id, exc)
            continue
    return list(out.values())
