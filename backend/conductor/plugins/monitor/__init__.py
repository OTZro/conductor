"""Monitor plugin — the host-metrics panel as a plugin tab. Self-contained: the panel
drives its own ``/api/metrics`` router directly (kept wired in main.py), so the tab has
no data provider — same pattern as the orchestrator."""

from __future__ import annotations

from .router import router as ROUTER  # noqa: F401 — mounted by the loader
from ..base import MenuBarSpec, Plugin, TabSpec

PLUGIN = Plugin(
    id="monitor",
    label="Mon",
    icon="∿",
    order=40,
    tab=TabSpec(layout="monitor"),  # provider=None → self-contained
    menu_bar=MenuBarSpec(layout="monitor-hosts"),  # compact Base/Roam status in the header
)
