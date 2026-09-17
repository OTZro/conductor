"""Regression coverage for PR #40 review findings (Codex + CodeRabbit) on the
M8 branch: a malformed LinkMatcherSpec result must not escape discover_links'
isolation."""

from __future__ import annotations

from conductor.links import discover_links
from conductor.plugins import LinkMatcherSpec
from conductor.plugins.runtime import SpecRegistration, spec_registry


def test_matcher_returning_malformed_entries_is_isolated_not_fatal():
    """A matcher whose result is missing `kind`/`ref` (or isn't a mapping at
    all) must be swallowed like a raising matcher — the KeyError/TypeError
    from consuming `found` must not escape discover_links, per LinkMatcherSpec's
    documented isolation contract (Codex + CodeRabbit, PR #40)."""

    def _malformed(text: str, jira_base_url: str) -> list:
        return [{"kind": "zz"}, "not-even-a-dict", 42]  # missing ref / non-mapping

    effect = spec_registry("link_matchers").register(
        SpecRegistration(plugin_id="zz-malformed", field="link_matchers",
                         specs=(LinkMatcherSpec(match=_malformed),))
    )
    try:
        # a real jira ref must still be found even though a sibling matcher's
        # RESULT (not just its call) is broken
        links = discover_links("see PROJ-1 for context", "https://x")
        assert any(l["kind"] == "jira" and l["ref"] == "PROJ-1" for l in links)
    finally:
        effect.dispose()
