# 🎼 Conductor

A single-user, localhost **Kanban cockpit** over Jira + GitHub + Slack, with a Claude
Code session per card.

Conductor is the cockpit that shows **what's stuck on you** and lets you resolve it in
one place — answer a parked ticket's question, approve/merge a PR, or drop into a live
Claude terminal.

```
  sources (truth)              aggregator (FastAPI + Postgres)        cockpit (browser)
  ───────────────              ───────────────────────────────       ──────────────────
  Jira (acli)  ─poll──▶                                              Kanban board
   in-flight + my holds      ┌───────────────┐                        ├ ★ Need Human   (ball: you)
  GitHub (gh)  ─poll──▶ ─────│ card store    │── WebSocket ─▶         ├ AI Working     (ball: ai)
   review-requested:@me      │ + lane rules  │                        ├ Backlog        (not started)
  Slack (token) ─poll──▶     │ + link auto-  │                        └ Done
   @mentions / DMs           │   discovery   │                       card detail drawer
  (optional) any other       │ (Postgres)    │                        ├ Awaiting Input → option buttons
  automation, via a          └──────┬────────┘                        ├ PR approve / merge
  `plugins/src_<name>/` ─poll─▶      │ actions                        ├ embedded terminal (ttyd)
   source plugin                     ▼                                 └ links: ticket / PR / Slack
                          Jira: comment + drop hold label
                          GitHub: gh review/merge
                          tmux + claude (attach / own)
```

**Sources are pluggable.** Jira/GitHub/Slack ship as first-party source plugins
(`backend/conductor/plugins/src_{jira,github,slack}/`); a fourth automation tool of
your own — anything that drops a status file or posts webhooks — can be wired in the
same way by writing a new `plugins/src_<name>/` package, without touching core. See
[docs/plugins.md](docs/plugins.md).

## Lanes

A card's lane is derived from one rule — *who holds the ball*:

| Signal | Lane | Ball |
|---|---|---|
| Jira hold label (`CONDUCTOR_JIRA_HOLD_LABEL`, default `conductor-hold`) — an automation parked it for you | **★ Need Human** | you |
| GitHub PR review-requested for you | **★ Need Human** | you |
| Jira assigned to you & **started** (In Progress / In Review / …) · manual note · Slack DM/@mention · a dashboard Claude waiting on you | **★ Need Human** | you |
| Jira assigned to you but **not started** (To Do / Backlog / Open / Triage / …) | **Backlog** | you |
| a source plugin signals it's actively working the card (e.g. reviewing a PR) | AI Working | ai |
| Done / merged / closed | Done | none |

**Backlog** holds tickets assigned to you that haven't started yet, split out so they
don't drown the urgent Need Human items; it's collapsible (starts open — Done starts
collapsed).

## Pin a card

Pin a card from its detail to **keep it on the board independent of your JQL** — a ticket
that's aged out of the query, resolved past the Done window, or one you just want to keep
in view. A pin is fetched on its own (so it shows right away) and persists until you unpin
it. It's just a stored ticket key, backed by `GET`/`POST`/`DELETE /api/pins`.

## Custom personal boards (local, per-user)

Add your own board tabs — for personal notes kept **off** the work board — by
dropping a JSON file at `~/.conductor/dashboards.json` (path via
`CONDUCTOR_DASHBOARDS_FILE`):

```json
[{ "id": "personal", "label": "Personal", "icon": "📝" }]
```

Each entry becomes a nav tab. A note **created while on that tab** is tagged to it
(`cached.manual.board`) and shows only there. The file lives **outside the repo**,
so it's private to your machine — teammates who clone Conductor get the mechanism
but none of your boards (no file = just the standard board). Everyone defines their
own.

Four more local override files follow the same pattern (outside the repo, defaults
shipped in code): `~/.conductor/termkeys.json` (mobile terminal soft-keys),
`~/.conductor/hotkeys.json` (**desktop physical-keyboard remap** — the ⌘/⌥/Ctrl+Shift
and Shift+Enter bindings xterm.js doesn't send itself; a non-empty file **replaces**
the shipped table rather than merging, so re-add any default you still want — schema
+ defaults in `frontend/src/components/TermKeys.tsx`), `~/.conductor/prompts.json`
(**the prompts Conductor sends to claude** — Slack/PR
briefs, session seeds, handover — keep yours in your own language while the shipped
defaults stay English; keys + placeholders in `backend/conductor/prompts.py`), and
`~/.conductor/lanes.json` (**extra board stages** — e.g. a manual "Pending" column:
`{"stages": [{"key": "pending", "title": "Pending"}]}`; move cards there from the
card detail's ⇢ Stage button. Custom stages only subdivide the human-owned space —
Done/AI Working always win — and a card's manual stage auto-clears when it reaches
Done. Full schema in `backend/conductor/lanes.py`). A ready-made Traditional-Chinese
prompts set ships in the repo — activate it with
`cp config/prompts.zh-TW.example.json ~/.conductor/prompts.json`.

## Plugins

Most tabs, some card-detail sections, and header menu-bar widgets are **plugins** —
discovered feature modules under `backend/conductor/plugins/`, rendered through named FE
layouts. A fresh clone ships **Monitor, Sandbox, Marketplace, and Files** (files a card's
claude sent via SendUserFile — captured by a PostToolUse hook, listed on the card);
**Orchestrator** is a
gitignored *local* demo (`plugins/local/`) — the pattern for a plugin you keep to your own
machine. Adding one that reuses an existing layout (e.g. embedding a web tool as an
`iframe` tab) is a backend module + a manifest, **no core edit**. See
[docs/plugins.md](docs/plugins.md) for the contract, the three contribution types (tab,
card-widget, menu-bar), layouts, and worked examples.

## Prerequisites

- **Host tools**: `docker` (OrbStack), `uv`, `node` 18+, `gh` (logged in), `acli`
  (logged in), `claude`, `tmux`, `ttyd` (>= 1.7.5 — earlier builds bundle an xterm.js
  without `Terminal.input`, and the mobile soft-keys then silently drop raw input).
  `bin/conductorctl bootstrap` checks all of
  these — **[docs/setup.md](docs/setup.md)** is the install guide (written for you or
  Claude Code to follow command-by-command).
- **Python** is fetched by `uv` (the repo pins 3.13 in `backend/.python-version`).
- **Node 18+** for the frontend.

> The backend, frontend, ttyd, tmux and claude all run **on the host** (they need
> host process access). Only Postgres runs in Docker.

## Setup

**Quickstart (macOS, recommended)** — one command takes a fresh clone to a running
cockpit: `.env` + backend venv + Postgres + frontend build + launchd. It **checks** host
tools but doesn't install them; install whatever it flags `✗` and re-run (it's
idempotent). See **[docs/setup.md](docs/setup.md)** for the tool install guide.
```bash
bin/conductorctl bootstrap    # check tools → .env, venv, postgres, FE build, launchd
open http://127.0.0.1:8787    # then log in so the board fills: gh auth login / acli jira auth login
```
`bin/conductorctl doctor` re-checks host deps + CLI auth anytime. The `bootstrap`/
`install`/`start` launchd path is macOS-only; on **Linux**, start Postgres with
`make db-up` and run the backend yourself (see **Run** + docs/setup.md) — everything
else is the same.

**Manual (dev / Linux):**
```bash
cp .env.example .env          # defaults work; Slack uid + name auto-derive from the token
make db-up                    # Postgres in Docker (:5433)
make backend-install          # uv venv + deps
make frontend-install         # npm install
```

## Run

**Dev (two terminals, hot reload):**
```bash
make backend     # FastAPI on :8787  (Vite proxies /api + /ws here)
make frontend    # Vite on :5173  →  open http://localhost:5173
```

**Single-process (backend serves the built SPA):**
```bash
cd frontend && npm run build      # emits frontend/dist
make backend                      # open http://127.0.0.1:8787
```

## Operating it (`conductorctl`)

On macOS the backend runs as a launchd agent (`co.conductor.backend`, `KeepAlive` — it
survives crashes, logout, and reboot). Manage it with `bin/conductorctl`:

| Command | What it does |
|---|---|
| `bootstrap` | fresh clone → running cockpit (check tools, then .env / venv / postgres / FE build / launchd) |
| `doctor` | check host deps + CLI auth (read-only) |
| `install` | (re)render the launchd agents + start; idempotent |
| `restart` | restart the backend to pick up new code — **the right way** (`launchctl kickstart`, never `pkill uvicorn`) |
| `start` / `stop` | start / stop the backend (Postgres stays up) |
| `status` | backend + Postgres + health at a glance |
| `logs` | tail the backend log (`~/.conductor/logs/backend.log`) |
| `db` | ensure the Postgres container is up |
| `backup` | dump + gzip the DB now (see below) |
| `setup` | lighter onboarding (.env + doctor + Postgres, no build) |

A second agent, **`co.conductor.backup`**, runs `conductorctl backup` daily at **03:30** —
a gzipped `pg_dump` into `~/.conductor/backups/`, pruned after 14 days. Both agents are
installed by `bootstrap` / `install`.

> After changing backend code, use `conductorctl restart` — **not** `make backend`, which
> would start a *second* uvicorn alongside the launchd one.
>
> ℹ️ `restart` reloads the **backend** and also **auto-rebuilds the frontend** when
> `frontend/dist` is older than `frontend/src` — so a `git pull` that touched `frontend/`
> no longer silently ships a stale UI (a vanished picker, a resurfaced hardcoded default).
> Backend-only restarts skip the build and stay instant; after a rebuild, reload the
> browser. (If `npm` isn't on `PATH`, it falls back to a warning.)

## Configuration (`.env`, all `CONDUCTOR_`-prefixed)

Highlights below; **`.env.example` is the exhaustive, commented list** (Slack noise
filters, polling cadences, terminal knobs). Defaults are placeholders that work out of
the box for a quick try — a real deployment reviews the rows tagged *(org)* /
*(machine)*. Misspelled `CONDUCTOR_*` keys (env or any env file) are flagged in the log
at startup instead of being silently ignored.

| Var | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `…@127.0.0.1:5433/conductor` | matches docker-compose |
| `JIRA_JQL` | *(see below)* | the board feed |
| `GITHUB_QUERY` | `is:open user-review-requested:@me` | gh search for your review-requested PRs |
| `SLACK_TOKEN` | — | user token `xoxp-…` (scopes `im:read im:history search:read users:read`) **or** a browser `xoxc-…` paired with `SLACK_COOKIE=xoxd-…`; blank = Slack off |
| `SLACK_USER_ID` | auto | derived from the token via `auth.test` — leave blank |
| `SLACK_INCLUDE_USERGROUPS` | — | allowlist of your @usergroups that count; blank = all of them (minus `SLACK_EXCLUDE_USERGROUPS`) |
| `SLACK_ALLOW_BOT_MENTIONS` | `false` | `true` = an app/bot post carrying a @group/@channel ping is a job too; a bot @-mentioning you directly always is |
| `SLACK_MENTION_QUERY_TEMPLATE` | `<@{id}>` | escape hatch for Slack search changes — the query used per target (`{id}` = user/usergroup id). Every hit is verified against it, so a wrong value under-catches instead of flooding |
| `JIRA_BASE_URL` | `your-org.atlassian.net` | *(org)* change for a different Jira site |
| `GITHUB_ORG` | `your-org` | *(org)* scopes the ticket→PR search |
| `GITHUB_LOGIN` | auto | your gh login; drives the PR card's "my review" status — blank auto-detects |
| `DEFAULT_WORKSPACE_ROOT` | `~/code` | *(machine)* where a card's claude runs with no explicit dir |
| `JIRA_HOLD_LABEL` | `conductor-hold` | the Jira label meaning "parked for you" → Need Human |
| `JIRA_TRD_FIELD` | — | Jira custom-field id (e.g. `customfield_XXXXX`) to surface a "Target Release Date"; blank = standard due date only |
| `BRIEF_MODEL` | `haiku` | model for one-shot Slack/PR summary briefs (cheap tier) |
| `DONE_PRUNE_DAYS` | `10` | drop cards parked in Done longer than this (keep in sync with the JQL's `-10d`) |
| `SLACK_MENTION_KEEP_DAYS` | `14` | drop un-actioned @-mention cards once the message is this old; `0` keeps them forever (they accumulate without bound — the Need Human lane fills with stale pings) |
| `NTFY_TOPIC` | — | optional ntfy.sh topic for Need-Human pings (macOS notifications are automatic) |
| `POLL_INTERVAL_S` | `30` | Jira/GitHub/Slack poll cadence |
| *auth block* | — | `AUTH_ENABLED`, `ALLOWED_EMAILS`, `GOOGLE_CLIENT_ID`, `AUTH_SETUP_TOKEN`, … → see **Authentication** (note: `ALLOWED_EMAILS` defaults to a personal address — change it) |
| *remote block* | — | `REMOTE_HOSTS`, `LOCAL_HOST_NAME`, `PUBLIC_URL`, `REMOTE_WORKSPACE_ROOT` → see **Remote host** |
| `SANDBOX_LINK_PORT` | `8000` | a sandbox VM's link `http://<vm>.orb.local:<port>/` — **plugin-owned** (read from the env by the Sandbox plugin, not a core setting, so it's not in `.env.example`) |

`JIRA_JQL` default (the exact string — `-10d` stays in sync with `DONE_PRUNE_DAYS`):
```
assignee = currentUser() AND issuetype not in (Epic, Initiative)
AND (status in (Committed, Building, Hardening) OR resolved >= -10d) ORDER BY updated DESC
```

## Terminal modes (per card)

- **Watch (attach)** — read-only `tmux attach` to a live session, for any source plugin
  that declares one via `cached.watch` on the card (no shipped plugin does today, so
  this stays dormant until you add one — see `docs/plugins.md`).
- **New Claude session** — a fresh interactive `claude` in its own tmux session, in the card's workspace.

Both render in an embedded ttyd terminal (or "open in new tab"). On mobile, a
soft-key bar (Esc/Tab/arrows/^C/tmux…) sits above the terminal — customise it with a local
`~/.conductor/termkeys.json` (`CONDUCTOR_TERMKEYS_FILE`), else the built-in defaults apply.

## Remote host (optional)

A **New Claude session** can run on another machine over ssh — e.g. `base` (this
always-on machine, the default) vs `roam` (a beefier laptop). Set
`CONDUCTOR_REMOTE_HOSTS=roam=<tailnet-name>` (+ `CONDUCTOR_PUBLIC_URL` for agent-state
hooks) and a host picker appears on the card. ttyd/ports/proxy stay local — the
remote session is just `ssh -t roam tmux …`, so a dropped ssh (laptop closed) leaves
tmux+claude alive there; reopen re-attaches, and the card remembers which machine its
claude session lives on (resume goes back to it). Watch is always local. Claude
conversations never move between machines.

## Authentication (optional)

Google login + WebAuthn **passkeys**, gated by an email **allowlist**
(`CONDUCTOR_ALLOWED_EMAILS`). When `CONDUCTOR_AUTH_ENABLED=true`, every API route,
`/ws`, and the `/term/*` proxy require a session cookie; the
static SPA stays open and renders a login screen. Sessions last 30 days
(`CONDUCTOR_SESSION_TTL_DAYS`) and are stored in Postgres — only a sha256 of the
cookie token is persisted. **Auth endpoints work even while enforcement is off**,
so you can register a passkey and test login *before* locking the door.

Enable it in this order (lockout-safe):

1. *(Optional — Google button)* Create a GCP OAuth client (type **Web
   application**). Authorized JavaScript origins: `http://localhost:8787`,
   `http://127.0.0.1:8787`, `http://localhost:5173`, and your
   `CONDUCTOR_PUBLIC_URL` (the tailscale-serve HTTPS origin). No redirect URI is
   needed (Google Identity Services button flow). Paste the client id into
   `CONDUCTOR_GOOGLE_CLIENT_ID`. Skip this entirely to use passkeys only.
2. **Register a passkey on the URL you'll actually log in from** *(before
   enabling)*. A passkey is bound to the **hostname** it's registered on, so use
   the origin you'll really sign in from:
   - **Remote (tailnet):** `https://<your-machine>.<tailnet>.ts.net/?auth-setup`
     (your `CONDUCTOR_PUBLIC_URL`) — this is the exposed surface, so register here.
   - **Local:** `http://localhost:8787/?auth-setup` — use **`localhost`, never
     `127.0.0.1`**: browsers reject IP-address passkeys with a `SecurityError`.

   On that page → *First-time setup (passkey)* → paste `CONDUCTOR_AUTH_SETUP_TOKEN`
   from `.env` + your allowlisted email → **Register**, then **Sign in with a
   passkey** to confirm login works. Register once per origin you use — a
   `localhost` passkey won't work over the tailnet and vice-versa. (Setting up
   Google in step 1 avoids this entirely: Google login works on any allowed origin.)
3. Set `CONDUCTOR_AUTH_ENABLED=true` and restart the backend (`conductorctl restart`, or
   `make backend` in dev).
4. **Lockout recovery** — any of these: set `CONDUCTOR_AUTH_ENABLED=false` and
   restart (the switch lives in `.env`, not the DB); or re-register via
   `?auth-setup` (the setup token keeps working); or use Google if you configured
   it. It's your machine — you can always get back in.
5. *(Optional hardening)* Once your passkey works, blank
   `CONDUCTOR_AUTH_SETUP_TOKEN` and restart to remove the bootstrap path — or leave
   it set as a recovery route (it only ever mints sessions for allowlisted emails).

Notes: additional passkeys
(other devices) can be added any time from the user chip in the header. Removing an
email from `CONDUCTOR_ALLOWED_EMAILS` blocks new logins but does **not** end its
existing 30-day sessions — delete the matching `auth_session` rows to force logout.

## Tests

```bash
make test     # full backend suite (800+ tests): lane/link, terminal, slack, github, auth, metrics
```
No Postgres/Docker needed — the auth tests spin up a throwaway sqlite file.

## Project layout

```
backend/conductor/
  plugins/   discovered modules — src_jira/src_github/src_slack (sources) ·
             sandbox · monitor · marketplace (+ gitignored local/ demo; see docs/plugins.md)
  actions/   resume (Awaiting Input → comment + drop hold) · pr · terminal (ttyd/tmux)
  api/       cards · board · actions · terminals · auth · dashboards · ws · plugins · term-proxy
             (pins lives on plugins/src_jira's router, not api/ — see docs/plugins.md)
  lanes.py   ball rule + stage registry (lanes.json/plugins)   links.py  auto-discovery   store.py  upsert   notify.py  pings
frontend/src/
  components/ Board · Lane · CardItem · CardDetail · LinkChips · PluginPanel · PluginCardWidgets
  plugins/    layout registry + tab/card layouts (the FE "primitive library")
config/      prompts.zh-TW.example.json (ready-made prompt overrides)
```

## Notes

- "Need Human" for a parked ticket is detected via the Jira hold label
  (`CONDUCTOR_JIRA_HOLD_LABEL`), so it requires the Jira poll.
