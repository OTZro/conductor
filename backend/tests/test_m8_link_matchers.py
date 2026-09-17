"""M8 step 2 contract: ``conductor.links.discover_links`` split into per-source
``LinkMatcherSpec`` declarations (src_jira / src_github / src_slack), collected
through the kernel's SpecRegistry — the same "core iterates a spec surface"
shape as ``LinkEnricherSpec`` (see test_kernel_specs.py's
``test_kernel_only_link_enricher_is_in_specs`` for the sibling pattern).

Behavioral identity of the three built-in matchers is already pinned by
test_logic.py's discover_links tests (unmodified); this file adds the NEW
seam-level proofs: a kernel-native matcher with no Plugin dataclass is served,
a broken matcher is isolated rather than breaking the upsert path, and the
three built-ins together still cover jira+pr+slack in one call (proving the
split recomposes to the pre-split behavior)."""

from __future__ import annotations

from conductor.links import discover_links
from conductor.plugins import LinkMatcherSpec
from conductor.plugins.runtime import SpecRegistration, spec_registry


def test_all_three_builtin_matchers_recompose_the_original_behavior():
    text = (
        "feat(PROJ-13493): see https://github.com/your-org/fms/pull/19804, "
        "also landed in your-org/fms#19804, discussed at "
        "https://your-org.slack.com/archives/C079Z6JLF2P/p1700000000000000"
    )
    links = discover_links(text, "https://your-org.atlassian.net")
    pairs = {(l["kind"], l["ref"]) for l in links}
    assert ("jira", "PROJ-13493") in pairs
    assert ("pr", "your-org/fms#19804") in pairs
    assert ("slack", "https://your-org.slack.com/archives/C079Z6JLF2P/p1700000000000000") in pairs
    # PR url + shorthand for the SAME ref must collapse to exactly one entry
    assert sum(1 for k, r in pairs if k == "pr") == 1


def test_kernel_native_matcher_with_no_plugin_dataclass_is_served():
    """A matcher registered straight into the SpecRegistry (no Plugin, no
    discovery) must be picked up by discover_links — proving core never
    hardcodes which plugins provide link_matchers."""

    def _match(text: str, jira_base_url: str) -> list[dict]:
        return [{"kind": "zzkind", "ref": "Z-1", "url": "https://z/Z-1", "title": "Z-1", "auto": True}]

    effect = spec_registry("link_matchers").register(
        SpecRegistration(plugin_id="zz-kern-links", field="link_matchers", specs=(LinkMatcherSpec(match=_match),))
    )
    try:
        links = discover_links("whatever text", "https://x")
        assert {"kind": "zzkind", "ref": "Z-1", "url": "https://z/Z-1", "title": "Z-1", "auto": True} in links
    finally:
        effect.dispose()


def test_broken_matcher_is_isolated_not_fatal():
    """A plugin's LinkMatcherSpec must not be able to break upsert_card's link
    discovery for every OTHER source — matches enrich.py's isolation stance."""

    def _boom(text: str, jira_base_url: str) -> list[dict]:
        raise RuntimeError("bad regex plugin")

    effect = spec_registry("link_matchers").register(
        SpecRegistration(plugin_id="zz-kern-boom", field="link_matchers", specs=(LinkMatcherSpec(match=_boom),))
    )
    try:
        # a real jira ref must still be found even though a sibling matcher blew up
        links = discover_links("see PROJ-1 for context", "https://x")
        assert any(l["kind"] == "jira" and l["ref"] == "PROJ-1" for l in links)
    finally:
        effect.dispose()


def test_empty_and_none_text_short_circuit_without_dispatch():
    assert discover_links(None, "https://x") == []
    assert discover_links("", "https://x") == []
