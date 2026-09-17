"""Slack source as a first-class plugin.

M5: slack polling moves out of main.py onto the kernel's PollSpec framework.
M8: the IMPLEMENTATION itself moved INTO this package (``impl.py``); the old
``conductor.sources.slack`` dotted path (a deprecation shim through the rest of
M8) was removed outright once its only consumers — test files — were
repointed here. This module owns only the cadence declaration.

- ``poll`` — the old ``_aux_loop``'s trailing ``slack`` step (DMs + mentions +
  broadcasts), each ``poll_interval_s``. It shared that loop for scheduling
  economy only; it reads/writes just the ``cached.slack`` namespace, so running
  it in its own loop preserves behavior. Without a configured token the poll is
  a cheap no-op (impl.poll returns 0), exactly as before.

The step records freshness under the LEGACY /api/status name "slack" via
status.run_step — that name is frozen API. Startup rides the poller framework's
fixed 10s settle delay (was 5s); steady-state cadence identical.

Disabling: list ``src_slack`` in ~/.conductor/plugins.json "disabled" and the
poll never registers.

M8: also declares this source's ``LinkMatcherSpec`` (``links.py``) — the Slack-
permalink half of core's free-text link discovery, split out of the old
monolithic ``conductor.links.discover_links``. See that module's docstring for
the collection point.
"""

from __future__ import annotations

from ...config import settings
from ...status import run_step
from ..base import LinkMatcherSpec, Plugin, PollSpec
from . import impl as slack  # noqa: F401 — re-exported: api layer routes through here
from .links import match_slack_links
from .router import router as ROUTER  # noqa: F401 — auto-mounted by the plugin loader


async def _poll() -> None:
    await run_step("slack", slack.poll)


PLUGIN = Plugin(
    id="src-slack",
    label="Slack Source",
    icon="💬",
    polls=(PollSpec(name="poll", fn=_poll, interval=float(settings.poll_interval_s), report_status=False),),
    link_matchers=(LinkMatcherSpec(match=match_slack_links),),
)
