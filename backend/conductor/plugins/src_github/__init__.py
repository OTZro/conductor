"""GitHub source as a first-class plugin.

M5: github polling moves out of main.py onto the kernel's PollSpec framework.
M8: the IMPLEMENTATION itself moved INTO this package (``impl.py``); the old
``conductor.sources.github`` dotted path (a deprecation shim through the rest
of M8) was removed outright once its only consumers — test files — were
repointed here. This module owns only the cadence declarations and the
loop-step composition.

THE SEQUENCE IS THE CONTRACT:

- ``aux`` — the github half of the old ``_aux_loop``, each ``poll_interval_s``,
  IN ORDER: ``github`` (my review-requested PRs) → ``github-pr-enrich`` (jira
  tickets' PR status) → ``github-pr-enrich-manual`` (PR links on slack/manual/
  plugin cards). The poll-before-enrich order within this source is deliberate
  and preserved as one sequence. The old loop's remaining steps (jira-links,
  link-enrich, slack) carried no data dependency on these — they touch disjoint
  cached namespaces — so they now ride their own loops (src_jira / core /
  src_slack).
- ``slow`` — the github steps of the old ``_dates_loop``, each
  ``dates_interval_s``, in order: ``github-pr-refresh`` (PR states,
  merged → Done) → ``github-pr-brief``. The old loop ran core's done-prune
  between them; that prune is age-gated (done_prune_days, store.prune_done), so
  a JUST-flipped card is never prune-eligible in the same tick — the
  refresh/prune relative order carried no same-cycle effect and the prune stays
  in core's housekeeping loop.

Every step records freshness under its LEGACY /api/status name via
status.run_step — those names are frozen API. Startup rides the poller
framework's fixed 10s settle delay (was 5s/8s); steady-state cadence identical.

Disabling: list ``src_github`` in ~/.conductor/plugins.json "disabled" and none
of these polls register; api endpoints importing this package keep working.

M8: also declares this source's ``LinkMatcherSpec`` (``links.py``) — the PR-
reference half (URL + ``owner/repo#N`` shorthand) of core's free-text link
discovery, split out of the old monolithic ``conductor.links.discover_links``.
See that module's docstring for the collection point.
"""

from __future__ import annotations

from ...config import settings
from ...status import run_step
from ..base import LinkMatcherSpec, Plugin, PollSpec
from . import impl as github  # noqa: F401 — re-exported: api layer routes through here
from .links import match_pr_links
from .router import router as ROUTER  # noqa: F401 — auto-mounted by the plugin loader


async def _aux() -> None:
    await run_step("github", github.poll)
    await run_step("github-pr-enrich", github.enrich_jira_prs)
    await run_step("github-pr-enrich-manual", github.enrich_manual_prs)


async def _slow() -> None:
    await run_step("github-pr-refresh", github.refresh_pr_states)
    await run_step("github-pr-brief", github.brief_prs)


PLUGIN = Plugin(
    id="src-github",
    label="GitHub Source",
    icon="🐙",
    polls=(
        PollSpec(name="aux", fn=_aux, interval=float(settings.poll_interval_s), report_status=False),
        PollSpec(name="slow", fn=_slow, interval=float(settings.dates_interval_s), report_status=False),
    ),
    link_matchers=(LinkMatcherSpec(match=match_pr_links),),
)
