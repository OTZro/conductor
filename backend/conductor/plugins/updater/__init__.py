"""Updater plugin — a header widget that lights up when the Conductor checkout is behind
its upstream and updates it in place on one click (git pull --ff-only + restart).

Self-contained, no core edit: a PollSpec does the `git fetch` + compare on a slow
cadence, the ROUTER serves status/check/apply, and the FE menu-bar layout renders the
badge + popover. No tab — it lives only in the header."""

from __future__ import annotations

from .router import router as ROUTER  # noqa: F401 — auto-mounted by the loader
from .service import refresh
from ..base import MenuBarSpec, Plugin, PollSpec

PLUGIN = Plugin(
    id="updater",
    label="Update",
    icon="⬆",
    menu_bar=MenuBarSpec(layout="updater"),
    # git fetch + compare every 30 min, so "an update is available" appears passively;
    # the widget's "check now" forces it sooner. Recorded under updater.check in /status.
    polls=(PollSpec(name="check", fn=refresh, interval=1800.0),),
)
