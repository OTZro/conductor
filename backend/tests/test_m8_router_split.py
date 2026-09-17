"""M8 step 3 contract: per-source HTTP endpoints moved out of ``conductor.api``
into their owning plugin's ``router.py``. URLs are FROZEN (pinned by the
existing behavioral suites — test_link_enrichers, test_pr_action_writeback,
test_stages, test_auth — which exercise these paths end to end and stay
untouched); this file adds the NEW seam-level proofs: (a) the live app's route
table is actually served by the plugin's router object — exactly once, by the
SAME endpoint function, not merely "a route with this path/method exists
somewhere" (a leftover core duplicate would pass a weaker check) — and (b)
disabling the owning plugin drops exactly its routes, by full path, and
nothing else's.

(Revised per PR #40 review — CodeRabbit correctly flagged that the original
version of this file (i) never checked live-route uniqueness/ownership, so a
reintroduced core duplicate would go undetected, and (ii) checked disablement
via the outer umbrella router's OWN ``.prefix`` (always "" for src_jira's
bare ``APIRouter()``), which is vacuously true whether or not src_jira was
scanned at all.)"""

from __future__ import annotations

from conductor.main import app
from conductor.plugins.src_github.router import router as github_router
from conductor.plugins.src_jira.router import router as jira_router
from conductor.plugins.src_slack.router import router as slack_router

FROZEN_ROUTES = [
    ("GET", "/api/jira/statuses"),
    ("POST", "/api/jira/{key}/transition"),
    ("GET", "/api/pins"),
    ("POST", "/api/pins"),
    ("DELETE", "/api/pins/{key}"),
    ("POST", "/api/cards/{card_id}/hold"),
    ("GET", "/api/cards/{card_id}/awaiting"),
    ("POST", "/api/cards/{card_id}/resume"),
    ("POST", "/api/cards/{card_id}/pr/{action}"),
    ("GET", "/api/cards/{card_id}/slack-thread"),
]

# which plugin router owns each frozen path — for the ownership/identity check
OWNER = {
    ("GET", "/api/jira/statuses"): "jira",
    ("POST", "/api/jira/{key}/transition"): "jira",
    ("GET", "/api/pins"): "jira",
    ("POST", "/api/pins"): "jira",
    ("DELETE", "/api/pins/{key}"): "jira",
    ("POST", "/api/cards/{card_id}/hold"): "jira",
    ("GET", "/api/cards/{card_id}/awaiting"): "jira",
    ("POST", "/api/cards/{card_id}/resume"): "jira",
    ("POST", "/api/cards/{card_id}/pr/{action}"): "github",
    ("GET", "/api/cards/{card_id}/slack-thread"): "slack",
}
ROUTERS = {"jira": jira_router, "github": github_router, "slack": slack_router}


def _flatten(routes):
    """FastAPI >=0.138 represents ``include_router`` lazily as ``_IncludedRouter``
    markers rather than copying routes in place — descend into
    ``original_router.routes`` to reach the real ``APIRoute`` objects."""
    for r in routes:
        original = getattr(r, "original_router", None)
        if original is not None:
            yield from _flatten(original.routes)
        else:
            yield r


def _endpoint_map(routes) -> dict[tuple[str, str], list]:
    """(method, path) -> every matching route's endpoint callable, so a
    duplicate registration is visible as len() > 1 instead of silently
    passing an `in` check."""
    out: dict[tuple[str, str], list] = {}
    for r in _flatten(routes):
        for method in getattr(r, "methods", ()):
            out.setdefault((method, r.path), []).append(r.endpoint)
    return out


def _route_map(routes) -> set[tuple[str, str]]:
    return set(_endpoint_map(routes))


def test_every_frozen_path_is_owned_exactly_once_by_its_plugin_router():
    """For each frozen path: exactly one live route serves it, and that
    route's endpoint IS the plugin router's own endpoint function — not just
    a path/method match that could be satisfied by an undetected duplicate."""
    live = _endpoint_map(app.routes)
    for method, path in FROZEN_ROUTES:
        live_endpoints = live.get((method, path), [])
        assert len(live_endpoints) == 1, (
            f"{method} {path}: expected exactly one live route, found "
            f"{len(live_endpoints)} — a duplicate registration would silently "
            f"shadow the plugin's own handler"
        )
        owner_endpoints = _endpoint_map(ROUTERS[OWNER[(method, path)]].routes).get((method, path), [])
        assert owner_endpoints, f"{method} {path}: not found on its declared owner router"
        assert live_endpoints[0] is owner_endpoints[0], (
            f"{method} {path}: the live app route is served by a DIFFERENT "
            f"function than the plugin router's own — something else is shadowing it"
        )


def test_disabling_src_jira_drops_exactly_its_paths():
    """Discovery-time disable (per plugins.json) must skip importing src_jira's
    module entirely, so none of its 8 URLs (3 prefixes: /api/jira, /api/pins,
    and 3 /api/cards/* actions) are reachable through its router — while a
    sibling source's paths are unaffected. Checked by FULL PATH, not by the
    umbrella router's own (always-empty) ``.prefix`` — src_jira's ``ROUTER``
    is a bare ``APIRouter()`` wrapping three sub-routers via
    ``include_router``, so its own prefix is "" whether or not it was scanned
    at all; only the flattened path set actually reflects presence/absence.

    Re-runs ``_scan`` directly (the ``test_kernel_shim`` idiom): every module is
    already imported, so this just re-derives (plugins, routers) with one more
    disabled — re-registration into the kernel is last-wins/idempotent for the
    unaffected modules, and the MODULES rows this appends are cleaned up after."""
    import conductor.plugins as plugins_pkg

    before = len(plugins_pkg.MODULES)
    plugins: dict = {}
    routers: list = []
    try:
        plugins_pkg._scan(
            plugins_pkg.__path__[0], plugins_pkg.__name__, plugins, routers, "shipped",
            {"src_jira"},
        )
        assert "src-jira" not in plugins

        paths: set[tuple[str, str]] = set()
        for r in routers:
            paths |= _route_map(r.routes)

        jira_only_paths = {
            ("GET", "/api/jira/statuses"),
            ("POST", "/api/jira/{key}/transition"),
            ("GET", "/api/pins"),
            ("POST", "/api/pins"),
            ("DELETE", "/api/pins/{key}"),
            ("POST", "/api/cards/{card_id}/hold"),
            ("GET", "/api/cards/{card_id}/awaiting"),
            ("POST", "/api/cards/{card_id}/resume"),
        }
        assert not (jira_only_paths & paths), jira_only_paths & paths

        # sibling sources are untouched by src_jira's disable
        assert "src-github" in plugins
        assert ("POST", "/api/cards/{card_id}/pr/{action}") in paths
        assert ("GET", "/api/cards/{card_id}/slack-thread") in paths
    finally:
        del plugins_pkg.MODULES[before:]
