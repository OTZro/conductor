# CLAUDE.md — Conductor

Working context for AI agents (and humans) in this repo. Product/config/setup docs
live in [README.md](README.md) — this file only carries what an agent needs up
front.

## What this is
A single-user localhost Kanban cockpit over Jira + GitHub + Slack, with a Claude Code
session per card. FastAPI + Postgres backend, React/Vite frontend; it shells out to
`acli`, `gh`, `claude`, `tmux`, `ttyd`. Everything **but Postgres runs on the host**
(they need host process access).

## Setup
Follow **[docs/setup.md](docs/setup.md)** — the step-by-step install runbook. Short
of it: `bin/conductorctl bootstrap`, install whatever it flags ✗, re-run until it
proceeds; the CLI logins (`gh` / `acli` / `claude`) are interactive and belong to
the human. Conductor acts as whoever's CLIs are logged in.

## Architecture (where to look)
- `backend/conductor/plugins/src_{jira,github,slack}/impl.py` — each has a
  `poll()` that fetches → normalizes → `store.upsert_card(cached_patch={...})`,
  namespaced under `cached.<source>`; that plugin's `__init__.py` declares the
  cadence as `PollSpec`s. **Add a source = write a new `plugins/src_<name>/`
  package (impl.py + `PLUGIN = Plugin(..., polls=(...))`)** — core never imports
  a source by name (M8: `conductor.sources` no longer exists at all).
- `backend/conductor/lanes.py::recompute_ball` — single source of truth for who
  holds the ball (`human` / `ai` / `none`) → lane. Precedence is an ordered chain.
- `backend/conductor/api/` — action + read endpoints. `config.py` — every
  `CONDUCTOR_*` setting + its default. `prompts.py` — the claude prompts
  (overridable via `~/.conductor/prompts.json`).
- `backend/conductor/plugins/` — self-contained tab/card-widget/menu-bar modules
  (see `docs/plugins.md`); `plugins/local/` is gitignored for personal ones.
- `frontend/src/` — `App.tsx` (shell), `components/` (Board, CardItem, CardDetail,
  TerminalsPanel), `plugins/` (FE layout registry), `cardMeta.ts` (status lines).

## Gotchas
- Only Postgres is Docker; the app runs on the host under launchd (macOS). Restart
  with `bin/conductorctl restart` — never `pkill uvicorn`, never a second
  `make backend` beside the launchd one.
- `.env` is gitignored — never commit tokens.
- Tests: `cd backend && pytest`; `npm --prefix frontend run build` also type-checks.
