"""example-plugin — a menu-bar clock. The whole point of this file is to be short: a
real plugin's ``PLUGIN`` declaration looks exactly like this, whatever it actually
does. See ``docs/plugins.md`` for the full contribution-type reference.

This package is a MARKETPLACE STARTER KIT, not a real conductor plugin — it lives
under docs/ and is never on conductor's plugin-discovery path. Copy this directory
out to a new git repo, rename it, tag it ``v1.0.0``, and it becomes installable via
the marketplace (either through a custom-repo paste, or by adding it to an index).

Imports the plugin base ABSOLUTELY, not via ``..base``: a shipped plugin lives at
``conductor.plugins.<name>``, where ``..base`` correctly reaches
``conductor.plugins.base`` — but the marketplace installs a repo's ``backend/`` one
level DEEPER, at ``conductor.plugins.local.<name>``, where ``..base`` resolves to the
nonexistent ``conductor.plugins.local.base`` and the loader silently skips the
plugin. The absolute path is correct at either depth.
"""

from __future__ import annotations

from conductor.plugins.base import MenuBarSpec, Plugin
from .router import router as ROUTER  # noqa: F401 — auto-mounted by the plugin loader

PLUGIN = Plugin(
    id="example-plugin",
    label="Example",
    icon="🕒",
    menu_bar=MenuBarSpec(layout="example-clock"),
)
