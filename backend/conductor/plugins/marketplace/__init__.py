"""Plugin marketplace — discover, install, update and remove THIRD-PARTY conductor
plugins from the UI, HACS-style.

Where this differs from the plugin manager (``plugins/manager``): the manager imports
an arbitrary zip or GitHub tarball with no version story. The marketplace's
distribution unit is a GIT REPO carrying a root ``conductor-plugin.json`` manifest
(name/version/description/author/backend/frontend/min_conductor); "version" is a
semver git tag, so update means "is there a newer tag", not "re-fetch and hope". A
repo's ``backend/`` copies to ``backend/conductor/plugins/local/<name>/`` and its
``frontend/`` to ``frontend/src/plugins/local/<name>/`` — the same local-plugin roots
the manager and the loader already know about, so an installed plugin is discovered
and loaded exactly like a hand-written one.

Plugins are discovered from one or more configurable index.json URLs
(``CONDUCTOR_MARKETPLACE_INDEX_URLS``) plus any custom repo the user pastes into the
Browse tab. Installing/updating/removing is restart-based, same honesty as the
manager: ``POST /apply`` rebuilds the frontend (only when something pending needs it)
and restarts via ``bin/conductorctl restart``'s own mechanism, never a signal to the
running process directly.

See ``docs/marketplace/`` for the index.json schema and the plugin-author starter kit.
"""

from __future__ import annotations

from ..base import Plugin, TabSpec
from .router import router as ROUTER  # noqa: F401 — auto-mounted by the plugin loader

PLUGIN = Plugin(
    id="marketplace",
    label="Marketplace",
    icon="⬡",  # plain glyph, not a colored emoji — matches manager's "⚙" / sandbox's "▤"
    order=91,  # beside the plugin manager — a management surface, not a daily destination
    tab=TabSpec(layout="plugin-marketplace"),  # self-contained — the panel drives ROUTER
)
