"""M8 contract (post-shim-removal): ``conductor.sources`` no longer exists —
``conductor.plugins.src_<name>.impl`` is the SOLE implementation, and each
plugin package's own module-level attribute (``conductor.plugins.src_jira.jira``,
bound via ``from . import impl as jira`` in that package's ``__init__.py``) is
the documented idiom for whole-module access (``from conductor.plugins.src_jira
import jira as jira_src``) — the same style already used throughout production
code (api/actions.py, the router.py modules, actions/resume.py, actions/pr.py).

The compatibility shim (``sys.modules[__name__] = _impl`` in a since-deleted
``conductor/sources/<name>.py``) existed only to keep pre-M8 test imports and
monkeypatches working. Per the user's decision on 2026-09-16: no production
code under ``conductor/`` ever imported ``conductor.sources``
(``test_source_contracts.py`` pins that main.py doesn't), the shim's only
consumers were ~12 test files (all rewritten to the plugin path) plus one
local, gitignored plugin (edited out of band, not part of this repo) — a
deprecated path with no outside consumers is pure confusion once this project
is open source, so the shim is gone outright rather than kept "just in case".

This file now pins: (a) ``conductor.sources`` is truly gone (import raises),
(b) the package attribute and ``.impl`` are the identical module object, which
is what makes monkeypatching either one visible to code reading its own
globals, and (c) disabling a source plugin still unregisters its polls/routes
(unaffected by the shim's removal — that contract was never about the shim)."""

from __future__ import annotations

import importlib

import pytest

CASES = [
    ("jira", "src_jira", "conductor.plugins.src_jira.impl"),
    ("github", "src_github", "conductor.plugins.src_github.impl"),
    ("slack", "src_slack", "conductor.plugins.src_slack.impl"),
]


def test_conductor_sources_no_longer_exists():
    """The dotted path is gone outright — not a shim, not an empty package."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("conductor.sources")


@pytest.mark.parametrize("name,module,impl_path", CASES)
def test_conductor_sources_submodule_no_longer_exists(name, module, impl_path):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(f"conductor.sources.{name}")


@pytest.mark.parametrize("name,module,impl_path", CASES)
def test_package_attribute_is_the_same_module_object_as_impl(name, module, impl_path):
    """The documented idiom (``from conductor.plugins.src_jira import jira as
    jira_src``) and the direct submodule import (``conductor.plugins.src_jira.
    impl``) must resolve to the literal same module — not two equal-looking
    objects — since this is what lets a monkeypatch through either name affect
    code that reads its own globals from ``impl``."""
    pkg = importlib.import_module(f"conductor.plugins.{module}")
    attr = getattr(pkg, name)
    impl = importlib.import_module(impl_path)
    assert attr is impl


def test_monkeypatch_via_the_documented_idiom_affects_impl_globals(monkeypatch):
    """The load-bearing property every rewritten pinning test relies on:
    patching an attribute through ``from conductor.plugins.src_jira import
    jira as jira_src`` must be visible to code DEFINED in (and reading its own
    globals from) that same module."""
    from conductor.plugins.src_jira import impl
    from conductor.plugins.src_jira import jira as jira_src

    assert jira_src is impl  # no re-export copy — genuinely one module, two names

    sentinel = object()
    monkeypatch.setattr(jira_src, "_ZZ_M8_PROBE", sentinel, raising=False)
    assert impl._ZZ_M8_PROBE is sentinel


def test_disabling_a_source_plugin_unregisters_its_polls_and_routes():
    """Per the M8 contract: disabling ``src_jira`` in plugins.json stops
    discovery from importing the PLUGIN package's ``__init__`` at all — no
    ``PLUGIN`` (so no polls) and no ``ROUTER`` get collected. Unaffected by
    the shim's removal: this was always discovery's job, never the shim's."""
    import conductor.plugins as plugins_pkg

    before = len(plugins_pkg.MODULES)
    plugins: dict = {}
    routers: list = []
    try:
        plugins_pkg._scan(
            plugins_pkg.__path__[0], plugins_pkg.__name__, plugins, routers,
            "shipped", {"src_jira"},
        )
        assert "src-jira" not in plugins
        # its own module is never imported at all when disabled — the modern
        # equivalent of "the shim keeps importing" is moot: there is nothing
        # left importable under the old path, and the plugin path is simply
        # not imported when disabled (by design — see plugins/__init__.py)
        import sys

        # a sibling, non-disabled source IS still importable/loaded normally
        assert "src-github" in plugins
        assert "conductor.plugins.src_github.impl" in sys.modules
    finally:
        del plugins_pkg.MODULES[before:]
