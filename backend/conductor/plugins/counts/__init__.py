"""Counts plugin — the header lane-count chips (need-human / backlog / AI-working, plus
any custom registry lane currently holding cards). Pure declaration: no router, no
provider. The FE layout renders straight from the `counts`/`lanes` core already
forwards to every menu-bar layout via PluginMenuBar — this plugin only decides WHETHER
the chips render, via the manager's enable/disable switch."""

from __future__ import annotations

from ..base import MenuBarSpec, Plugin

PLUGIN = Plugin(
    id="counts",
    label="Counts",
    icon="#",
    order=5,  # header-only (no tab), but keep it early alongside the other lane chrome
    menu_bar=MenuBarSpec(layout="counts"),
)
