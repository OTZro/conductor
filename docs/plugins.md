# Plugins

Conductor's tabs and some card-detail sections are **plugins** — self-contained
feature modules discovered at startup, not wired into the core. Adding one that reuses
an existing layout is drop-in: a backend module + a manifest, **no edit to `App.tsx` or
`CardDetail.tsx`**. Today Monitor, Sandbox and Marketplace ship as plugins (Orchestrator is a
gitignored local demo — see below); only Board and Terminals (plus your custom dashboards)
are built in.

## The shape of a plugin

A plugin is a module **or package** under `backend/conductor/plugins/` exposing a
module-level `PLUGIN` (and optionally `ROUTER`, a FastAPI `APIRouter` the app mounts —
how a self-contained plugin brings its own backend endpoints):

```python
# backend/conductor/plugins/<name>/__init__.py   (a package; a single .py also works)
from ..base import (  # import what you use — all contribution types live in base
    Plugin, TabSpec, CardWidgetSpec, MenuBarSpec, HookSpec, StageSpec,
    LaneChangeSpec, StateProbeSpec, PollSpec, NotifySpec, LinkEnricherSpec,
)
from .router import router as ROUTER   # optional — auto-mounted by the loader

PLUGIN = Plugin(
    id="my-thing",          # url/view id + nav shortcut source
    label="Mine",           # nav label
    icon="◆",               # nav icon
    order=60,               # nav position (built-ins occupy 0–40; default 100 = append)
    tab=...,                # optional: a full-tab contribution (below)
    card_widget=...,        # optional: a card-detail section (below)
    menu_bar=...,           # optional: a header widget (below)
)
```

A self-contained plugin keeps **all** its code in its package — manifest, router, data
logic, FE layouts — with nothing plugin-specific in core (the shipped `sandbox` /
`monitor` / `marketplace` are packages; `orchestrator` is the gitignored local demo).
Discovery (`plugins/__init__.py`) scans two roots — the shipped `plugins/` and a
**gitignored `plugins/local/`** (a personal "drop your own plugin here" dir, absent for
other clones) — collecting each module's `PLUGIN` + `ROUTER`. A module that raises on
import is logged and skipped.

The **frontend never hard-codes a plugin.** `App.tsx` fetches `/api/plugins/manifest`,
appends each plugin's tab to the nav (sorted by `order`), renders via `PluginPanel`, and
`CardDetail` drops in `PluginCardWidgets`. Layouts come from each plugin's FE package
`src/plugins/<name>/index.{ts,tsx}` — merged into the registry via `import.meta.glob`
(shipped or gitignored `local/`). `registry.tsx` holds only generic primitives (`iframe`).

## Contribution types

### Tab

A full tab. Three flavours, all via `TabSpec(layout, provider=None, config={})`:

| flavour | how | example |
|---|---|---|
| **data-driven** | `provider` returns JSON; the named layout renders it | `sandbox` → `sandbox-hosts` |
| **self-contained** | `provider=None`; the layout is a full component that owns its own data/actions (talks to its own router) | `orchestrator`, `monitor` |
| **iframe** | `layout="iframe"`, `config={"url": …}`; embeds a URL, zero FE code | any local dashboard your own tool serves, reverse-proxied at a path of your choosing |

The FE renders a tab by looking `layout` up in `TAB_LAYOUTS`
(`frontend/src/plugins/registry.tsx`). `PluginPanel` owns the poll + container for
data-driven tabs; self-contained/iframe layouts render bare and own their whole surface.

### Card-widget

A section injected into a card's detail, contextual to that card:

```python
async def _card(ctx: dict) -> dict | None:
    # ctx = {origin, external_id, links: [{kind, ref}]}
    # return a dict to render, or None to show nothing for this card
    ...

card_widget=CardWidgetSpec(title="Sandbox", layout="sandbox-card", provider=_card)
```

The **provider owns the "does this apply?" logic** — the FE just renders whatever
non-`None` comes back, under `title`, via the `CARD_LAYOUTS[layout]` renderer.

`slot` picks WHERE in the detail it renders: `"body"` (default) is a titled section
below the card content; **`"above-body"` / `"below-body"`** hug the card's own content
(jira description / slack message) — for a section whose meaning depends on sitting
next to it, e.g. a "what happened last, what's needed from you" recap above it or a
translation of it directly below; `"actions"` renders the layout chrome-less **inline in
the header action row** (next to Snooze/Pin) — for compact controls; `"menu"` renders as
a **row inside the terminal's ⋯ overflow menu** — for session-scoped verbs. Worked
examples: the stages plugin (`slot="actions"`, a HoverCard button driving
`PATCH /state`) and the fork plugin (`slot="menu"`, a row that types claude's own
`/fork` into the live pane and opens a second terminal on the forked conversation).

### Claude hook

Observe a card's claude session without touching core: declare which hook event (and
optionally which tool) you want, and core folds it into every session's generated hook
settings + auto-exempts your endpoint from the auth gate. The hook's stdin payload is
forwarded verbatim to your own router, tagged `?card_id=&host=` (host = the machine the
session runs on, so you can read files locally or over ssh via `runner._run`).

```python
hooks=(HookSpec(event="PostToolUse", matcher="SendUserFile",
                path="/api/plugins/files/capture"),)
```

See `plugins/files/` for the full pattern (capture endpoint, ingest-token guard,
`store.touch_card` for live refresh).

### Board stage + lane-change

Contribute an extra board lane, and/or subscribe to lane transitions:

```python
stages=(StageSpec(key="review-queue", title="Review Queue", order=6),)
lane_changes=(LaneChangeSpec(callback=_on_lane_change),)  # ctx: {card_id, origin,
                                                          #  external_id, old_lane,
                                                          #  new_lane, ball}
```

Stages merge into one registry with the built-ins and the user's
`~/.conductor/lanes.json` (built-in > config > plugin on key collision); the FE builds
its columns from `GET /api/board/lanes`, so no frontend change. The **invariant**: ball
(who holds it — human/ai/none) stays core-derived and unoverridable; custom stages only
re-bucket cards *inside* one ball space (v1: `within="human"` only — the field exists so
ai-space pipeline columns later are a guard removal, not a redesign).

A plugin **places** a card by writing a stage claim into stored state — never via a
callback in the compute path:

```python
await upsert_card(session, origin=o, external_id=e,
                  cached_patch={"stages": {"my-plugin": "review-queue"}})   # claim
await upsert_card(session, origin=o, external_id=e,
                  cached_patch={"stages": {"my-plugin": None}})             # clear
```

Arbitration is fixed: the user's manual move (`PATCH /state {stage}`; the shipped
`stages` plugin provides the card-detail UI for it — core ships no stage UI; a manual
park sticks even while a claude waits, the waiting shows on the card itself) > live
agent-waiting > plugin claims (earliest display-ordered claimed stage wins) > the
built-in derivation. Lane-change callbacks are fire-and-forget, timeout-bounded, and
error-isolated; they MUST be idempotent (events only fire on real transitions, but two
plugins flapping opposing claims would still ping-pong).

### State probe

Extra "actually working" evidence for the pane poll (`StateProbeSpec` — see
`plugins/base.py`; the motivating case is an agent-team lead idling while teammates
work). Promote-only: a probe can flip waiting → running, never the reverse.

### Link enricher

Per-ref hover meta for a link KIND, without writing a loop:

```python
link_enrichers=(LinkEnricherSpec(kind="zendesk", fetch=_fetch),)
# _fetch(ref) -> {"title": …, "status": …, "lines": [...]} | None
```

Core finds the cards carrying such links, throttles per card, calls `fetch` per ref
(bounded), and snapshots into `cached.linkmeta[kind][ref]`; both link surfaces (board
chips + the detail's LinksPanel) render a generic hover from it. Built-in pr/jira
hovers stay bespoke — the jira one even carries a **⇄ status control** (transition the
ticket straight from the hover; the picker offers the board's real status vocabulary
and jira validates the move server-side).

### Poll

A periodic job run by core's poller framework — same error isolation and staleness
tracking as the built-in source polls:

```python
polls=(PollSpec(name="sync", fn=_sync, interval=60),)
```

Each run is recorded under `<plugin-id>.<name>` in `/api/status`, so the frontend
health dot judges the plugin's freshness exactly like jira/github/slack. `fn` errors
are logged + recorded, never fatal.

### Notify channel

An extra medium for the need-human ping, beside the built-in macOS/ntfy ones:

```python
notifiers=(NotifySpec(callback=_send_telegram),)
# ctx: {event: "need_human", title, subtitle, ref, link}
```

Fire-and-forget, timeout-bounded, error-isolated; fires only after the boot seed
(`notify.arm()`), so plugin channels don't get the startup burst either.

### Card badge (convention, not a spec)

Decorate board cards with zero fetch and zero per-plugin FE code — write stored data:

```python
await upsert_card(session, origin=o, external_id=e,
                  cached_patch={"badges": {"my-plugin": {"text": "3/8", "tone": "ai",
                                                         "title": "team tasks"}}})
# clear: {"badges": {"my-plugin": None}}
```

`CardItem` renders every `cached.badges.<plugin>` entry as a chip in the meta row
(`tone`: neutral/ok/warn/urgent/ai/human/done, defaults neutral). Same philosophy as
stage claims: decoration is data, not a callback in the render path.

## Recipe: a new SOURCE as a plugin

A data source (Linear, GitLab, PagerDuty, …) is just three existing pieces — no
`SourceSpec` needed and no core edit:

```python
async def _poll():
    items = await fetch_upstream()
    for item in items:
        async with session_maker() as s:
            await upsert_card(
                s, origin="linear", external_id=item.key, title=item.title,
                url=item.url,
                cached_patch={"linear": {"signal": {
                    "state": "needs_me" if item.assigned_to_me else
                             "working" if item.agent_running else
                             "done" if item.closed else None,
                    "detail": item.status_text,      # → the card's status sentence
                }}},
            )
    await prune_source("linear", keep={item.key for item in items})

PLUGIN = Plugin(id="linear", label="Linear", icon="◧",
                polls=(PollSpec(name="poll", fn=_poll, interval=30),))
```

1. **PollSpec** runs the loop with health tracking.
2. **The signal vocabulary** (`cached.<origin>.signal = {state, detail}`) drives ball
   at the same precedence ranks as the built-ins: `done` is terminal, `working` sits
   at the AI-working rank, `needs_me` beside assignee-me. `detail` becomes
   `agent_state`, which the FE status sentence falls back to — and an unknown origin
   gets a generic chip from its own name.
3. **`prune_source(origin, keep)`** drops cards the upstream no longer returns.

Cards from such a source get lanes/stages/snooze/pin/notifications/terminal sessions
for free — they're ordinary cards. Built-in sources keep their richer bespoke signals;
the vocabulary is the floor, not the ceiling.

Four refinements make a source card fully native:

- **Display convention** — `cached_patch={"display": {"label": "LIN-42", "ts":
  "2026-07-22T08:00:00Z"}}`: `label` replaces the raw external_id on the card (full id
  stays on hover) and `ts` (ISO-8601) becomes the card's shown time instead of
  updated_at — the same privileges slack's bespoke handling gets, as stored data.
- **PR link chips with live status** — attach the item's PRs via
  `upsert_card(extra_links=[{"kind": "pr", "ref": "owner/repo#123", "url": …}])`, or
  just let `link_text` auto-discover them. Every PR chip carries the same CI/review
  hover a jira card's does — however the link got there (the `enrich_manual_prs` poll
  + drawer-open refresh keep `cached.prs` fresh, throttled per card).
- **Card body** — `Plugin.card_body(ctx) -> {"kind", "content"} | None` supplies the
  detail's main content (what jira description / slack text are for the built-ins).
  Provider owns "does this apply"; first non-None wins; errors fall back to summary.
- **Enrichment signals (promote-only)** — flag ANOTHER source's card as needing the
  human: `cached_patch={"signals": {"<contributor>": {"state": "needs_me", "detail":
  "error spike"}}}` (clear with None). Deliberately StateProbe's philosophy: a foreign
  contributor may only pull a card TOWARD human — `done`/`working` are ignored (another
  source must not close your cards or claim AI holds them) — and it slots below every
  live/owning signal, so contributors can't reorder the precedence chain. Deterministic
  among several: first by contributor name.

## Layouts — the FE "primitive library"

A *layout* is a named renderer in `frontend/src/plugins/registry.tsx`. Reusing one costs
no FE code; a genuinely new look is added **once** and then any plugin can declare it.

```tsx
export const TAB_LAYOUTS = {
  "sandbox-hosts": SandboxHostsLayout,   // data-driven: grouped host tiles
  orchestrator: () => <OrchestratorPanel />,  // self-contained component
  monitor: () => <MonitorPanel />,
  iframe: IframeLayout,                   // generic: embeds config.url
};
export const CARD_LAYOUTS = {
  "sandbox-card": SandboxCardLayout,
};
```

A tab layout receives `{ data, config, cards, onOpenCard }` (`PluginTabProps`); a card
layout receives `{ data }` (`PluginCardProps`).

## Worked examples

**1. Embed any web tool as a tab (zero FE code)** — this is the whole plugin:

```python
PLUGIN = Plugin(id="grafana", label="Graf", icon="📊", order=70,
                tab=TabSpec(layout="iframe", config={"url": "http://localhost:3000/"}))
```

**2. A data tab** — a provider + a named layout (write the layout once, in `registry.tsx`):

```python
async def _rows(): return await something()          # → your layout's shape
PLUGIN = Plugin(id="foo", label="Foo", icon="◇",
                tab=TabSpec(layout="foo-table", provider=_rows))
```

**3. A card-detail section** — see `plugins/sandbox.py` for the full pattern (match the
card's ticket → return data or `None`).

## Endpoints

| endpoint | purpose |
|---|---|
| `GET /api/plugins/manifest` | every plugin's declaration (the FE reads this) |
| `GET /api/plugins/{id}/tab` | a tab's data (empty for self-contained) |
| `POST /api/plugins/{id}/card` | a card-widget's data for one card context (`{data: …|null}`) |
| `GET /api/board/lanes` | the merged stage registry (built-ins + lanes.json + plugin stages) |

## Not covered yet (deliberate boundaries)

Not yet built (each needs a host-side hook, added when a real plugin needs it):

- **Card hiding/filtering** — a plugin removing cards from the board (decoration IS
  supported — see "Card badge" above).
- **ai-space stages** — `StageSpec(within="ai")` is guarded off until a real
  team-pipeline plugin needs columns inside AI Working.

*(Backend router discovery, claude hooks, board stages, lane-change subscriptions,
state probes, polls, notify channels and card badges are all supported — see the
contribution types above. Interactive card-widgets are too: a card layout is a full
component and may own actions — see `plugins/stages/`, whose layout drives
`PATCH /state` directly.)*

A feature needing a bespoke React UI beyond these layouts still requires either a new
named layout (added once, then reusable) or the iframe path — there's no runtime custom
React without a rebuild.
