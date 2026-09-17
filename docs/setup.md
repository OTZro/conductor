# Setup — from a fresh clone to a running cockpit

`bin/conductorctl bootstrap` does everything automatable (`.env`, backend venv, Postgres,
frontend build, launchd). The one thing it does **not** do is install host tools — it
**checks** for them and stops if any are missing. Installing them is a deliberate manual
step. This doc is the install guide for that step, written so **you or Claude Code** can
follow it command-by-command.

## The loop

```
bin/conductorctl bootstrap      # checks host tools; if any ✗, stops and lists them
# install the ✗ tools using the table below
bin/conductorctl bootstrap      # re-run — it picks up where it left off
```

Re-running is safe (idempotent): an existing `.env` is kept, `uv sync` / `npm ci` are
no-ops when nothing changed, and the launchd install just restarts the backend.

> **If you are Claude Code doing this setup:** run `bin/conductorctl bootstrap`, read each
> `✗ <tool>` line, install that tool with the command in the table below, then re-run
> bootstrap. Repeat until it proceeds past the check. The three **auth** steps
> (`gh auth login`, `acli jira auth login`, `claude` sign-in) open a browser and are
> interactive — you cannot complete them headless, so hand those back to the human.

## Host tools

All commands are macOS / Homebrew (the `install`/`start` path is launchd = macOS). Install
[Homebrew](https://brew.sh) first if `brew` is missing. Linux notes at the bottom.

| Tool | What Conductor uses it for | Install |
|---|---|---|
| **docker** | runs Postgres in a container (the only Dockerised piece) | `brew install orbstack` (or Docker Desktop) |
| **uv** | backend venv + fetches Python 3.13 | `curl -LsSf https://astral.sh/uv/install.sh \| sh` (or `brew install uv`) |
| **node** | frontend build (`npm ci && npm run build`) | `brew install node` |
| **gh** | GitHub PR polling + review/merge actions | `brew install gh` → then **auth** below |
| **acli** | Jira polling + comment/hold actions | [Atlassian ACLI installer](https://developer.atlassian.com/cloud/acli/guides/install-acli/) → then **auth** |
| **claude** | per-card Claude Code sessions + AI briefs | `npm install -g @anthropic-ai/claude-code` (or `curl -fsSL https://claude.ai/install.sh \| bash`) |
| **tmux** | terminal sessions Conductor attaches to | `brew install tmux` |
| **ttyd** | renders those terminals in the browser | `brew install ttyd` |

Python itself is **not** a separate install — `uv` fetches the 3.13 pinned in
`backend/.python-version` when it builds the venv.

## Auth (interactive — a human runs these)

The tools above must be logged in, or the board comes up empty. These open a browser, so
they can't be scripted:

```bash
gh auth login                 # GitHub
acli jira auth login          # Jira (Atlassian)
claude                        # first run signs you in to Claude Code
```

`bin/conductorctl doctor` (and `bootstrap`) report auth status as `·` reminders — they
never block on it, since Conductor runs fine and just shows an empty board until you log
in.

## Review the machine / org-specific config

`bootstrap` copies `.env.example` → `.env` on first run. The defaults are placeholders and
work out of the box for a quick try, but a real deployment should review these (each is a
real `CONDUCTOR_`-prefixed env var — no code editing):

| Var | Default | Change it when… |
|---|---|---|
| `CONDUCTOR_JIRA_BASE_URL` | `https://your-org.atlassian.net` | your Jira is a different site |
| `CONDUCTOR_JIRA_JQL` | active statuses (Committed/Building/Hardening) + holds + recently-done | your workflow uses different status names, or you want a saved team filter |
| `CONDUCTOR_GITHUB_ORG` | `your-org` | your repos live in another org |
| `CONDUCTOR_DEFAULT_WORKSPACE_ROOT` | `~/code` | your code lives elsewhere |
| `CONDUCTOR_SLACK_TOKEN` | *(blank)* | you want Slack mentions/DMs on the board (optional) |

Everything else in `.env.example` has a working default or is optional (Slack filters,
ntfy, …) — see the inline comments. Two optional extras have their own sections below:
**remote host (Roam)** and **authentication**.

## What `bootstrap` runs after the checks pass

1. `.env` ← `.env.example` (only if absent)
2. `uv sync --extra dev` in `backend/` → `backend/.venv`
3. `docker compose up -d postgres` (waits for `pg_isready`)
4. `npm ci && npm run build` in `frontend/` → `frontend/dist` (served by the backend)
5. `conductorctl install` → launchd agents (`co.conductor.backend` KeepAlive +
   `co.conductor.backup` @03:30); the backend creates the DB schema on first start
6. open **http://127.0.0.1:8787**

## Optional: remote host (Roam)

Conductor runs every card's Claude on the machine it's installed on (`base`). You can add a
second machine — e.g. `roam`, a beefier laptop — and pick it per card when starting a
**New Claude session** (Watch always stays on `base`).

Requirement: `base` reaches `roam` over **non-interactive ssh** (tailscale ssh, or a key
with no passphrase prompt). Test it first — `ssh <roam-target> true` must return instantly
with no prompt.

In `.env`:
```bash
CONDUCTOR_REMOTE_HOSTS=roam=<roam ssh target>        # e.g. roam=my-laptop.tailnet.ts.net
CONDUCTOR_LOCAL_HOST_NAME=base                        # display name of THIS machine
CONDUCTOR_PUBLIC_URL=https://<base>.<tailnet>.ts.net  # base's origin AS REACHABLE FROM roam
CONDUCTOR_REMOTE_WORKSPACE_ROOT=~/code                # default working dir on roam
```
Then `bin/conductorctl restart` — a host picker appears on the card.

- `CONDUCTOR_PUBLIC_URL` lets the remote Claude's hooks post agent-state back so the lane
  updates live. Blank ⇒ remote sessions run hook-less (the tmux poll still tracks the lane,
  just slower).
- ttyd / ports / proxy stay on `base`; the remote session is just `ssh -t roam tmux …`, so a
  dropped ssh (laptop closed) leaves tmux+claude alive on `roam` — reopen re-attaches, and
  the card remembers which machine its session lives on.

(README **"Remote host"** has the full behavior notes.)

## Optional: authentication (expose beyond localhost)

By default Conductor is localhost-only and unauthenticated — fine on your own machine. If
you expose it (e.g. `tailscale serve` over your tailnet), turn on **Google login + WebAuthn
passkeys**, gated by an email allowlist. The full ceremony (Google client, per-origin
passkey binding, lockout recovery, origins) is in the README **"Authentication"** section;
the essentials:

**Register a passkey BEFORE flipping the switch** — auth endpoints work while enforcement is
off, on purpose, so you can't lock yourself out:

1. In `.env`, set the allowlist + a one-time setup token (keep `AUTH_ENABLED=false` for now):
   ```bash
   CONDUCTOR_ALLOWED_EMAILS=you@example.com
   CONDUCTOR_AUTH_SETUP_TOKEN=$(python3 -c "import secrets;print(secrets.token_urlsafe(24))")
   # optional Google button: CONDUCTOR_GOOGLE_CLIENT_ID=<GCP OAuth "Web application" client id>
   ```
   `bin/conductorctl restart`.
2. Open the login URL you'll actually use and register a passkey — a passkey is bound to the
   **hostname**:
   - Local: `http://localhost:8787/?auth-setup` — use **`localhost`, never `127.0.0.1`**
     (browsers reject IP-address passkeys).
   - Remote: `https://<base>.<tailnet>.ts.net/?auth-setup` (your `CONDUCTOR_PUBLIC_URL`).

   → *First-time setup (passkey)* → paste the setup token + your allowlisted email →
   **Register**, then **Sign in** to confirm it works.
3. Set `CONDUCTOR_AUTH_ENABLED=true` and `bin/conductorctl restart`.
4. **Locked out?** Set `CONDUCTOR_AUTH_ENABLED=false` + restart — the switch lives in `.env`,
   not the DB, so you can always get back in.

## Linux

`install` / `start` / `bootstrap`'s launchd step is macOS-only. On Linux, do the same
pieces by hand:

```bash
cp .env.example .env
make db-up                    # Postgres in Docker (:5433)
make backend-install          # uv venv + deps
make frontend-install && (cd frontend && npm run build)
make backend                  # uvicorn :8787 (run it under your own supervisor)
```
