from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TabSpec:
    """A plugin's full-tab contribution: a named frontend layout + an optional async
    data provider. The layout string maps to a renderer the FE ships (its "primitive"
    library). ``provider=None`` marks a SELF-CONTAINED tab — its layout is a full
    component that talks to its own backend router directly (e.g. the orchestrator), so
    the FE renders it without a data fetch."""

    layout: str
    provider: Callable[[], Awaitable[Any]] | None = None
    config: dict = field(default_factory=dict)  # layout-specific, e.g. {"url": ...} for iframe


@dataclass(frozen=True)
class CardWidgetSpec:
    """A plugin's card-detail contribution: given a card's context, the provider returns
    a data dict to render via the named layout — or None to show nothing for this card.
    The provider owns the "does this apply?" logic, so the FE just renders whatever
    comes back.

    ``slot`` picks WHERE in the detail it renders: ``"body"`` (default) is a titled
    section below the card content; ``"above-body"`` / ``"below-body"`` are titled
    sections hugging the card's own content (description / slack message) — for a
    section whose meaning depends on sitting next to it, e.g. a "what happened last,
    what's needed from you" recap above it or a translation of it directly below;
    ``"actions"`` renders the layout inline in the header action row (next to
    Snooze/Pin/…), chrome-less — for compact controls like the stages plugin's move
    button; ``"menu"`` renders inside the terminal's ⋯ overflow menu as a row — for
    session-scoped verbs like the fork plugin's."""

    title: str
    layout: str
    provider: Callable[[dict], Awaitable[Any | None]]
    slot: str = "body"  # body | above-body | below-body | actions | menu


@dataclass(frozen=True)
class MenuBarSpec:
    """A plugin's menu-bar (header) contribution: a named FE layout rendered in the top
    bar. Self-contained — the layout fetches its own data (e.g. the monitor menu widget
    reads /api/metrics) and can jump to its own tab on click."""

    layout: str
    config: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StateProbeSpec:
    """A plugin's agent-state contribution: extra evidence that a live session is
    actually WORKING when its pane looks idle. The pane poll (agent_state) calls PROBE
    for each session it classified as waiting — ctx carries ``{"name": tmux session,
    "host": host-or-None, "card_id": ..., "was_running": bool}`` — and the probe returns
    ``("running", detail)`` to overrule (detail is a short card label, e.g. ``"team 3/8
    tasks"``) or ``None`` to abstain. The motivating case: an agent-team lead idles at
    its prompt while its teammates work — the pane says waiting, the team's task list
    says otherwise. A probe can only PROMOTE to running, never demote, and a live choice
    dialog (claude asking the human) always wins over a probe. Probes ride the 5s poll
    loop, so they must be fast; core bounds each call with a timeout and treats errors
    as abstain — a broken plugin must not stall or flip the board."""

    probe: Callable[[dict], Awaitable[tuple[str, str | None] | None]]


@dataclass(frozen=True)
class StageSpec:
    """A plugin's board-stage contribution: an extra lane on the main board, merged into
    the stage registry alongside the built-ins and the user's ``~/.conductor/lanes.json``
    (built-in > config > plugin on key collision). ``within`` names the ball space this
    stage subdivides — the INVARIANT of the stage system is that ball itself (who holds
    it: human/ai/none) stays core-derived and unoverridable; stages only re-bucket cards
    inside one ball space. v1 accepts only ``within="human"``; the field exists so
    opening the ai space later (e.g. team-pipeline columns) is a guard removal, not a
    schema change.

    A plugin PLACES a card in its stage by writing a claim into the card's stored state:
    ``upsert_card(..., cached_patch={"stages": {"<plugin-id>": "<stage-key>"}})`` (clear
    with None). Lane computation stays a pure sync function reading those claims — a
    plugin controls placement through data, never through a callback in the compute
    path. Manual moves (LocalState.manual_stage) always beat plugin claims."""

    key: str  # slug, e.g. "pending" — must not collide with a built-in lane
    title: str
    within: str = "human"  # ball space this stage subdivides; v1: only "human"
    tone: str = "backlog"  # FE accent: human | ai | done | backlog
    order: int = 5  # display position (built-ins sit at 0/10/20/30)
    rank: int | None = None  # flat-list sort priority; None → same as order
    collapsible: bool = False
    aging: bool = False  # show the need-human-style aging badge on cards here


@dataclass(frozen=True)
class LaneChangeSpec:
    """A plugin's lane-transition subscription: CALLBACK fires (fire-and-forget, bounded
    by a timeout, errors dropped) whenever a card actually changes lane, with ctx
    ``{card_id, origin, external_id, old_lane, new_lane, ball}``. This is the trigger
    surface for stage machinery — e.g. an orchestrator spawning the next agent when a
    card lands in its stage. Callbacks MUST be idempotent: one that writes a stage claim
    re-enters lane evaluation (events only fire on REAL transitions, which bounds it,
    but two plugins flapping opposing claims would still ping-pong)."""

    callback: Callable[[dict], Awaitable[None]]


@dataclass(frozen=True)
class PollSpec:
    """A plugin's periodic job, run by core's poller framework with the SAME error
    isolation and staleness tracking the built-in source polls get: each run is
    recorded under ``<plugin-id>.<name>`` in /api/status, so the frontend health dot
    judges the plugin's freshness exactly like jira/github/slack. ``fn`` errors are
    logged and recorded, never fatal; the loop sleeps ``interval`` seconds between
    completions. This (plus writing card data through ``upsert_card`` with the signal
    vocabulary — see docs/plugins.md "a new source") is how a plugin becomes a fully
    fledged data source with no core edit."""

    name: str  # status id suffix; full name is "<plugin-id>.<name>"
    fn: Callable[[], Awaitable[Any]]
    interval: float = 30.0  # seconds between completions
    # False ⇒ the loop runs on schedule but records NO "<plugin-id>.<name>" row in
    # /api/status. For composite fns whose STEPS already report under their own
    # (legacy) poller names via status.run_step — the aggregate row would be
    # tautologically green (run_step swallows step errors), pure noise.
    report_status: bool = True


@dataclass(frozen=True)
class NotifySpec:
    """A plugin notification channel: CALLBACK receives each need-human event
    (fire-and-forget, timeout-bounded, error-isolated) alongside the built-in
    macOS/ntfy pings — e.g. a Slack-DM or Telegram channel. ctx:
    ``{event: "need_human", title, subtitle, ref, link}`` (link is the deep-link to
    the card, None without public_url). Fires only after ``notify.arm()`` — the
    boot-burst suppression applies to plugin channels too."""

    callback: Callable[[dict], Awaitable[None]]


@dataclass(frozen=True)
class LinkEnricherSpec:
    """A plugin's link-kind enrichment: cards carrying links of KIND get per-ref hover
    meta without the plugin writing its own loop. Core finds the cards, throttles per
    card, calls FETCH per ref (bounded), and snapshots results into
    ``cached.linkmeta[kind][ref]``; the FE renders a generic hover (title / status /
    lines) for any kind without a bespoke body. Built-in pr/jira enrichment stays
    bespoke — this is the floor for NEW kinds (zendesk tickets, sentry issues, figma
    files, …). One enricher per kind (first discovered wins)."""

    kind: str  # CardLink.kind this enriches, e.g. "zendesk"
    fetch: Callable[[str], Awaitable[dict | None]]  # ref -> {title?, status?, lines?} | None


@dataclass(frozen=True)
class LinkMatcherSpec:
    """A plugin's free-text link-discovery contribution: ``match(text,
    jira_base_url)`` returns the list of link dicts (``{kind, ref, url, title,
    auto}``) THIS source recognizes in free text — e.g. src_jira matches ticket
    keys, src_github matches PR urls/shorthand, src_slack matches permalinks.
    core.links.discover_links (called from every ``upsert_card`` with
    ``link_text``) collects every registered matcher's results and dedupes by
    (kind, ref) — first registrant wins a collision, matching the pre-M8
    single-function's insertion-order semantics. CPU-only by contract (regex,
    no I/O): this runs on the hot upsert path, not a poll loop."""

    match: Callable[[str, str], list[dict]]


@dataclass(frozen=True)
class HookSpec:
    """A plugin's claude-hook contribution: on the given claude hook EVENT (optionally
    only for a tool MATCHER), Conductor forwards the hook's stdin payload to PATH — a
    Conductor route the plugin's own ROUTER serves. Core wires it into every session's
    generated hook settings and auto-exempts PATH from the session gate, so a plugin can
    observe claude's activity (a sent file, a spawned subagent, …) WITHOUT touching core.
    The forwarded request carries ``?card_id=&host=`` so the plugin knows which card and
    which machine the session runs on (to read a file locally or over ssh)."""

    event: str  # UserPromptSubmit | PreToolUse | PostToolUse | Notification | SessionEnd
    path: str  # where to POST the payload, e.g. "/api/plugins/files/capture"
    matcher: str | None = None  # optional claude tool-name matcher, e.g. "SendUserFile"


# The three FE render slots a plugin can visually contribute to, whose relative
# order is user-configurable from the plugin manager panel (see plugins/manager's
# "版面排序" section). Names match the Plugin dataclass fields they gate one-to-one,
# so `getattr(plugin, slot)` is how membership in a slot is tested.
RENDER_SLOTS: tuple[str, ...] = ("tab", "card_widget", "menu_bar")


@dataclass(frozen=True)
class Plugin:
    """A self-contained feature. Contributes any of the known extension points (a tab,
    a card widget, a menu-bar widget, claude hooks; more added as the framework grows).
    Discovered by module, not wired into core, so adding one is drop-in on the backend."""

    id: str
    label: str
    icon: str
    tab: TabSpec | None = None
    card_widget: CardWidgetSpec | None = None
    menu_bar: MenuBarSpec | None = None
    hooks: tuple[HookSpec, ...] = ()  # claude hooks this plugin wants forwarded to it
    state_probes: tuple[StateProbeSpec, ...] = ()  # extra running/waiting evidence (backend-only)
    stages: tuple[StageSpec, ...] = ()  # board lanes this plugin contributes
    lane_changes: tuple[LaneChangeSpec, ...] = ()  # lane-transition subscriptions (backend-only)
    polls: tuple[PollSpec, ...] = ()  # periodic jobs run by core's poller framework
    link_enrichers: tuple[LinkEnricherSpec, ...] = ()  # per-link-kind hover meta (backend-only)
    link_matchers: tuple[LinkMatcherSpec, ...] = ()  # free-text link discovery (backend-only)
    notifiers: tuple[NotifySpec, ...] = ()  # need-human notification channels (backend-only)
    # card BODY provider — the main content of a card's detail (what jira description /
    # slack text are for the built-ins). Called for cards no built-in handler claims;
    # gets ctx {origin, external_id}, returns {"kind": …, "content": …} or None to pass
    # (the provider owns "does this apply", like a card_widget's). First non-None wins.
    card_body: Callable[[dict], Awaitable[dict | None]] | None = None
    order: int = 100  # nav position; built-ins occupy 0–50, so plugins append by default

    def manifest(self) -> dict:
        """The FE-facing declaration — WHAT to render, not how to fetch it."""
        return {
            "id": self.id,
            "label": self.label,
            "icon": self.icon,
            "order": self.order,
            "tab": (
                {
                    "layout": self.tab.layout,
                    "self_contained": self.tab.provider is None,
                    "config": self.tab.config,
                }
                if self.tab
                else None
            ),
            "card_widget": (
                {
                    "title": self.card_widget.title,
                    "layout": self.card_widget.layout,
                    "slot": self.card_widget.slot,
                }
                if self.card_widget
                else None
            ),
            "menu_bar": (
                {"layout": self.menu_bar.layout, "config": self.menu_bar.config}
                if self.menu_bar
                else None
            ),
        }
