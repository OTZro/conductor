"""Plugin manager — see, switch, share and install plugins from the UI.

What it manages is the MODULE DIRECTORY, not the loaded Plugin object: the loader keys
its disable list by directory name so a plugin that fails to import can still be turned
off, and this plugin follows that choice everywhere.

The lifecycle is deliberately restart-based, not live. Backend plugins execute at
import time and frontend layouts are collected by ``import.meta.glob`` at BUILD time,
so "live disable" would be a lie — the honest contract is: edits accumulate, a banner
says a restart is pending, and one button applies everything (frontend rebuild included
when it matters). ``apply`` reuses the updater's detached-restart pattern.

Sharing is a zip whose layout mirrors the two roots a plugin lives in::

    backend/<name>/**    frontend/<name>/**    conductor-plugin.json

Import accepts that same layout, from a file or from a GitHub repo (``gh api`` tarball,
so the org auth already on this machine is what gates private repos). A GitHub import
records its provenance in ~/.conductor/plugins-sources.json — which is what makes
"update" possible at all: re-fetch the same repo. A bare zip has no provenance, so it
installs fine but can never answer "is there a newer version?".
"""

from __future__ import annotations

from ..base import Plugin, TabSpec
from .router import router as ROUTER  # noqa: F401 — auto-mounted by the loader

PLUGIN = Plugin(
    id="manager",
    label="Plugins",
    icon="⚙",
    order=90,  # last: a management surface, not a daily destination
    tab=TabSpec(layout="plugin-manager"),  # self-contained — the panel drives ROUTER
)
