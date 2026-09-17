"""Jira source as a first-class plugin.

M5: the board's jira polling moves out of main.py onto the kernel's PollSpec
framework. M8: the IMPLEMENTATION itself moved INTO this package (``impl.py``);
the old ``conductor.sources.jira`` dotted path (a deprecation shim through the
rest of M8) was removed outright once its only consumers — test files — were
repointed here, since a migration path nobody outside this repo ever lived
through is pure confusion once this project is open source. This module owns
the cadence declarations and the loop-step composition.

THE SEQUENCE IS THE CONTRACT:

- ``poll``  — the old ``_jira_loop``: the board search each ``poll_interval_s``,
  in its OWN poll so a slow/hung GitHub/enrich never delays seeing a ticket's
  status change. After the FIRST completed cycle it arms need-human
  notifications (``notify.arm``) exactly like the old loop did — the initial
  seed must never fire a boot burst of pings.
- ``links`` — the old ``_aux_loop``'s ``jira-links`` step (linked-jira hover
  enrichment), each ``poll_interval_s``. It shared a loop with the github/slack
  steps purely for scheduling economy — it reads/writes only ``cached.jiras`` /
  ``jira_link_ts``, so it carries no cross-source ordering dependency and is
  safe to run in its own loop.
- ``dates`` — the old ``_dates_loop``'s ``jira-dates`` step (due date / Target
  Release Date sweep) each ``dates_interval_s``; slow per-ticket ``view`` calls,
  kept out of the hot poll deliberately (unchanged).

Every step records freshness under its LEGACY /api/status name ("jira",
"jira-links", "jira-dates") via status.run_step — those names are frozen API.
Startup: plugin polls share the poller framework's fixed 10s settle delay
(main._plugin_poll_loop), replacing the old per-loop 2/5/8s staggers; steady-
state cadence is identical.

Disabling: list ``src_jira`` in ~/.conductor/plugins.json "disabled" and
discovery never registers these polls — the board simply stops polling jira.
The api endpoints that import this package still work; they are user-initiated,
not polling.

M8: also declares this source's ``LinkMatcherSpec`` (``links.py``) — the jira-
ticket-key half of core's free-text link discovery, split out of the old
monolithic ``conductor.links.discover_links``. See that module's docstring for
the collection point.
"""

from __future__ import annotations

import logging

from ... import notify
from ...config import settings
from ...status import run_step
from ..base import LinkMatcherSpec, Plugin, PollSpec
from . import impl as jira  # noqa: F401 — re-exported: api layer routes through here
from .links import match_jira_links
from .router import router as ROUTER  # noqa: F401 — auto-mounted by the plugin loader

log = logging.getLogger("conductor.plugins.src_jira")

_armed = False


async def _poll() -> None:
    global _armed
    await run_step("jira", jira.poll)
    if not _armed:
        # never let arming notifications kill the poll loop
        try:
            notify.arm()  # initial seed done — enable Need-Human notifications
        except Exception as exc:  # noqa: BLE001
            log.warning("[jira] notify.arm failed: %s", exc)
        _armed = True


async def _links() -> None:
    await run_step("jira-links", jira.enrich_linked_jiras)


async def _dates() -> None:
    await run_step("jira-dates", jira.poll_dates)


PLUGIN = Plugin(
    id="src-jira",
    label="Jira Source",
    icon="🎫",
    polls=(
        PollSpec(name="poll", fn=_poll, interval=float(settings.poll_interval_s), report_status=False),
        PollSpec(name="links", fn=_links, interval=float(settings.poll_interval_s), report_status=False),
        PollSpec(name="dates", fn=_dates, interval=float(settings.dates_interval_s), report_status=False),
    ),
    link_matchers=(LinkMatcherSpec(match=match_jira_links),),
)
