from __future__ import annotations

import logging
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# later entries win in pydantic-settings; real environment variables beat all files
_ENV_FILES: tuple[str, ...] = (".env", "../.env")


class Settings(BaseSettings):
    # read .env from either the repo root or backend/ (uvicorn runs from backend/)
    model_config = SettingsConfigDict(
        env_prefix="CONDUCTOR_", env_file=_ENV_FILES, extra="ignore"
    )

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 8787

    # --- database ---
    database_url: str = (
        "postgresql+asyncpg://conductor:conductor@127.0.0.1:5433/conductor"
    )

    # --- Jira via acli ---
    acli_bin: str = "acli"
    jira_base_url: str = "https://your-org.atlassian.net"
    # Board feed, scoped to the tickets actively being worked: assigned to whoever's
    # acli is logged in, in one of the active statuses (Committed / Building /
    # Hardening), work items only (no Epic / Initiative). We deliberately do NOT
    # exclude the hold label — a parked ticket stays in Building yet holds the ball
    # for YOU, so it IS the Need Human lane; excluding it would empty that lane, which
    # is the whole point here. We OR in recently-resolved (resolved >= -10d) so the
    # Done lane isn't empty. Keep -10d in sync with done_prune_days. Scope to a team
    # board by prefixing a saved filter, e.g.
    # CONDUCTOR_JIRA_JQL="filter = <id> AND assignee = currentUser() AND …".
    jira_jql: str = (
        "assignee = currentUser() "
        "AND issuetype not in (Epic, Initiative) "
        "AND (status in (Committed, Building, Hardening) OR resolved >= -10d) "
        "ORDER BY updated DESC"
    )
    jira_hold_label: str = "conductor-hold"
    # "Due date" is the standard `duedate` field. "Target Release Date" is a
    # custom field whose id varies per Jira instance — set CONDUCTOR_JIRA_TRD_FIELD
    # (e.g. customfield_XXXXX) to surface it; blank = only show the hard Due date.
    jira_trd_field: str = ""

    # --- GitHub via gh ---
    gh_bin: str = "gh"
    github_query: str = "is:open user-review-requested:@me"
    github_org: str = "your-org"  # scope ticket→PR search (cuts global gh-search junk)
    # my github login — drives the PR card's "my review" status (an automation tool can
    # post reviews under my own account, so a review by this login = my verdict,
    # distinct from other bots / copilot / coderabbit). Blank → auto-detect via
    # `gh api user`.
    github_login: str = ""

    # --- Slack (optional) ---
    slack_token: str | None = None
    slack_user_id: str | None = None
    slack_cookie: str | None = None  # xoxd-… when slack_token is a browser xoxc- token
    slack_max_age_days: int = 7  # only mentions/DMs newer than this become jobs
    # An un-actioned @-mention is deliberately kept PAST the surfacing window above, so
    # a real ask ("host a postmortem") doesn't silently vanish the moment it drops out
    # of search — but not forever. Past this many days it's pruned. Measured from the
    # message's own slack ts, not from when it was carded. 0 — or any value <= 0 —
    # keeps them indefinitely (the pre-2026-08 behavior); un-actioned mentions then
    # accumulate without bound, which is what made the board carry thousands of them.
    #
    # Only meaningful ABOVE slack_max_age_days. Pruning runs over the ids the poll did
    # NOT just surface (store.prune_source skips everything in `keep`), so while a
    # mention is still inside the surfacing window it is re-upserted every cycle and
    # cannot expire. Set this below slack_max_age_days and the surfacing window becomes
    # the effective floor. That is deliberate: pruning a card the same poll just wrote
    # would delete and re-create it every cycle, losing its LocalState (read, dismissed,
    # pinned, snoozed) each round via delete_card_cascade.
    slack_mention_keep_days: int = 14
    slack_dms: bool = True  # ingest direct messages too (false = @-mentions only)
    slack_exclude_channels: str = ""  # comma-separated channel names to skip entirely (no @ counts)
    # @usergroup handles to skip even though I'm a member — high-traffic groups whose
    # pings are noise, not tasks. Empty by default; set per your team, e.g.
    # CONDUCTOR_SLACK_EXCLUDE_USERGROUPS=release-management
    slack_exclude_usergroups: str = ""
    # @usergroup pings I'm a member of (@develop…) are always surfaced. This adds the
    # channel-wide @channel/@here/@everyone broadcasts in channels I'm in — off by
    # default because they're mostly FYI announcements (≈60/wk here) that bury the
    # board. Set true (CONDUCTOR_SLACK_BROADCASTS=true) to opt in.
    slack_broadcasts: bool = False
    # channel-name prefixes where ONLY a direct @me becomes a job — a usergroup/@channel
    # ping in these is noise, not a task addressed to me. A full channel name is a valid
    # (exact) prefix. Empty by default; set per your noise, e.g.
    # CONDUCTOR_SLACK_DIRECT_ONLY_PREFIXES=feed-,release_mgmt  (feed-* firehoses; a
    # release channel where only a direct ping to you matters).
    slack_direct_only_prefixes: str = ""
    # exact channel names where channel-wide broadcasts (@channel/@here/@everyone) are
    # dropped, but a direct @me AND @usergroup pings still count — a team's channel whose
    # broadcasts are noise to me, yet I still want to be pinged directly (e.g. team-mazu).
    slack_broadcast_exclude_channels: str = ""
    # The search.messages query used to find "someone pinged me", with {id} filled in
    # per target (my user id, then each of my usergroup ids). The default is the form a
    # mention REALLY takes in a message body — the one Slack matches literally.
    #
    # This is an escape hatch, not a preference: Slack changed its search behaviour under
    # us in 2026-08 (the `<!subteam^S…>` markup its Web API documents stopped matching and
    # silently degraded to a 1.7M-hit fuzzy match, flooding the board with lunch chatter).
    # Overriding this lets a self-hosted instance chase such a change without waiting for
    # a release. Getting it wrong can only produce FEWER cards, never a flood: every hit
    # is verified to contain the rendered query before it becomes one (see _handle_match).
    slack_mention_query_template: str = "<@{id}>"
    # @usergroup handles that count, as an allowlist — when set, ONLY these of my groups
    # are searched (exclude list still applies on top). Empty = every group I'm in, which
    # is the right default for a small team but noisy at nine groups. Comma-separated,
    # leading @ optional: CONDUCTOR_SLACK_INCLUDE_USERGROUPS=rd-team,value-expansion
    slack_include_usergroups: str = ""
    # Whether an app/bot post that carries a group or broadcast ping becomes a job. Off:
    # jira / google_calendar / sentry announcements swept up by an @group are FYI, not
    # someone asking me for something. A bot that @-mentions me DIRECTLY always counts,
    # regardless of this setting — that is a real ask addressed to me.
    slack_allow_bot_mentions: bool = False

    @property
    def slack_excluded(self) -> set[str]:
        return {c.strip() for c in self.slack_exclude_channels.split(",") if c.strip()}

    @property
    def slack_excluded_groups(self) -> set[str]:
        return {g.strip().lstrip("@").lower() for g in self.slack_exclude_usergroups.split(",") if g.strip()}

    @property
    def slack_included_groups(self) -> set[str]:
        """Allowlisted @usergroup handles (lowercased, @-stripped); empty = no allowlist."""
        return {g.strip().lstrip("@").lower() for g in self.slack_include_usergroups.split(",") if g.strip()}

    @property
    def slack_direct_only(self) -> tuple[str, ...]:
        return tuple(p.strip() for p in self.slack_direct_only_prefixes.split(",") if p.strip())

    @property
    def slack_broadcast_exclude(self) -> set[str]:
        return {c.strip() for c in self.slack_broadcast_exclude_channels.split(",") if c.strip()}

    # --- notifications (optional) ---
    ntfy_topic: str | None = None  # ntfy.sh topic for cross-device Need-Human pings

    # custom personal boards, defined in a LOCAL file (outside the repo, so each user
    # has their own without touching shared code). See api/dashboards.py.
    dashboards_file: Path = Path.home() / ".conductor" / "dashboards.json"
    # mobile terminal soft-keys, optional LOCAL override file. Absent → built-in
    # defaults (Esc/Tab/arrows/^C/tmux…). See api/termkeys.py.
    termkeys_file: Path = Path.home() / ".conductor" / "termkeys.json"
    # desktop physical-keyboard remap (⌘←/⌥⌫/⌃⇧V…), optional LOCAL override file.
    # Absent → built-in defaults in TermKeys.tsx. See api/termkeys.py.
    hotkeys_file: Path = Path.home() / ".conductor" / "hotkeys.json"
    # Claude prompts (briefs, session seeds, handover), optional LOCAL override file —
    # keep your own language/phrasing per machine. Absent → shipped English defaults.
    # See prompts.py for the keys.
    prompts_file: Path = Path.home() / ".conductor" / "prompts.json"
    # Colour themes (extra light/dark palettes), optional LOCAL file — same user-owned
    # pattern as prompts.json. Absent → the built-in dark + light pair only. A theme also
    # carries the embedded terminal's xterm palette and optional tmux styles, so one
    # choice re-skins the app, the terminal and its status bar together. See api/themes.py.
    themes_file: Path = Path.home() / ".conductor" / "themes.json"
    # Custom board stages (extra lanes), optional LOCAL file — same user-owned pattern
    # as prompts.json. Absent → the four built-in lanes only. See lanes.py stage_registry.
    lanes_file: Path = Path.home() / ".conductor" / "lanes.json"

    # Named launch presets (host / cwd / env) for a NEW card session, optional LOCAL
    # file — same user-owned pattern as prompts.json. Absent → feature off. See
    # launch_profiles.py.
    launch_profiles_file: Path = Path.home() / ".conductor" / "profiles.json"

    # --- terminals ---
    tmux_bin: str = "tmux"
    ttyd_bin: str = "ttyd"
    claude_bin: str = "claude"
    ttyd_port_start: int = 7500
    terminal_font_size: int = 14
    # default dir to run claude in when the field is blank and there's no other workspace hint
    default_workspace_root: Path = Path.home() / "code"
    # How to scroll a FULL-SCREEN pane (claude, vim, less) — one that runs on the
    # alternate screen, where tmux holds no scrollback of its own:
    #   "wheel" — forward mouse-wheel events to the app, which scrolls its own
    #             transcript line by line (what native tmux does for an app that
    #             requests mouse reporting: `mouse_any_flag=1` → `send-keys -M`)
    #   "page"  — send the app's PageUp/PageDown, one screen at a time
    # Flip this to compare without a rebuild; it only affects alternate-screen panes.
    # Default stays "page": it is the behaviour the shipped tests pin, and the
    # wheel path depends on the app actually honouring mouse reporting.
    alt_scroll_mode: str = "page"
    # Named tmux sockets (separate `-L <socket>` servers) to surface in the Terminals list,
    # beyond the default socket. Comma-separated; each item is `<socket>` (read-only — the
    # safe default) or `<socket>:rw` / `<socket>:ro`, so read-only is chosen PER socket, not
    # globally. E.g. CONDUCTOR_EXTRA_TMUX_SOCKETS=agents:rw surfaces an agent team's members
    # (member-* on `tmux -L agents`) and lets you type into them. A member's pane has an
    # automated writer (the team's daemon pastes the message + presses Enter), so `:rw` means you
    # accept colliding with it; omit the mode (or `:ro`) to just watch. Socket names must
    # not contain ':'. Empty ⇒ default socket only — zero behaviour change for an unset install.
    extra_tmux_sockets: str = ""

    @property
    def extra_tmux_socket_map(self) -> dict[str, bool]:
        """Parse extra_tmux_sockets → {socket_name: read_only}. `<name>` and `<name>:ro`
        (and any unrecognised mode) ⇒ read-only (the safe default); only an explicit
        `<name>:rw` ⇒ writable. Blanks dropped; a later duplicate wins."""
        out: dict[str, bool] = {}
        for item in self.extra_tmux_sockets.split(","):
            item = item.strip()
            if not item:
                continue
            name, _, mode = item.partition(":")
            name = name.strip()
            if name:
                out[name] = mode.strip().lower() != "rw"
        return out

    @property
    def extra_tmux_socket_list(self) -> tuple[str, ...]:
        """Just the socket names to sweep (the keys of extra_tmux_socket_map)."""
        return tuple(self.extra_tmux_socket_map)

    # --- remote hosts (run a card's claude/tmux on another machine over ssh) ---
    # Display name of THIS machine (the one running Conductor).
    local_host_name: str = "base"
    # Machines claude can run on, comma-separated "name=ssh-target", e.g.
    # "roam=chang-mbp.tailnet.ts.net". ssh must be non-interactive (tailscale ssh /
    # keys). Empty ⇒ feature off, everything runs locally as before.
    remote_hosts: str = ""
    # Conductor's origin AS REACHABLE FROM the remote hosts (e.g. the tailscale-serve
    # HTTPS URL). Remote claude hooks post agent state here; blank ⇒ remote sessions
    # get no hooks and lane state relies on the tmux pane poll alone.
    public_url: str = ""
    # default working dir for sessions on a remote host (a path on ITS filesystem)
    remote_workspace_root: str = "~"


    @property
    def remote_host_map(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for part in self.remote_hosts.split(","):
            name, _, target = part.strip().partition("=")
            if name.strip() and target.strip():
                out[name.strip()] = target.strip()
        return out

    # --- polling ---
    poll_interval_s: int = 30
    tail_interval_s: float = 2.0
    agent_poll_s: int = 5  # ground-truth agent-state read from live claude panes
    # pane captures allowed in flight PER HOST during one agent poll. Each one is a
    # local tmux subprocess or an ssh channel, so an unbounded fan-out over a large
    # session set can exhaust processes, fds or the remote's MaxSessions — and the
    # panes that then fail are exactly the ones whose cards go stale. Per host, not
    # global, so one slow remote can't starve the local captures.
    agent_poll_max_concurrent: int = 8
    dates_interval_s: int = 300  # due-date / target-release refresh (rarely changes)
    done_prune_days: int = 10  # drop cards parked in Done longer than this
    # model for the one-shot summarization briefs (slack mention / PR). The cheap
    # tier is indistinguishable for summaries and an order of magnitude cheaper
    # than letting `claude -p` default to the daily-driver model.
    brief_model: str = "haiku"

    # --- auth (Google login + WebAuthn passkeys — see README "Authentication") ---
    # Auth endpoints are live even when enforcement is off, so you can pre-register
    # a passkey and test login BEFORE flipping auth_enabled=true (lockout safety).
    auth_enabled: bool = False
    # comma-separated login allowlist, compared case-insensitively
    allowed_emails: str = "you@example.com"
    google_client_id: str = ""  # empty → Google sign-in button hidden
    auth_setup_token: str = ""  # empty → first-time passkey bootstrap disabled
    session_ttl_days: int = 30
    # comma-separated exact origins allowed for login ceremonies; empty → derived:
    # http://localhost:{port}, http://127.0.0.1:{port}, the Vite dev origins, and
    # public_url (the tailscale-serve HTTPS origin) when set.
    allowed_origins: str = ""
    # optional Bearer token for session-exempt, token-guarded ingest endpoints (e.g. the
    # claude hook callbacks in _hook_cfg); empty → open (back-compat)
    ingest_token: str = ""

    # --- plugin marketplace ---
    # comma-separated raw index.json URLs (e.g. a GitHub raw link); empty → no
    # curated index, browse still works via "add a custom repo" in the UI
    marketplace_index_urls: str = ""

    @property
    def allowed_email_set(self) -> set[str]:
        return {e.strip().lower() for e in self.allowed_emails.split(",") if e.strip()}

    @property
    def allowed_origin_set(self) -> set[str]:
        if self.allowed_origins.strip():
            return {o.strip().rstrip("/") for o in self.allowed_origins.split(",") if o.strip()}
        out = {
            f"http://localhost:{self.port}",
            f"http://127.0.0.1:{self.port}",
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        }
        if self.public_url:
            out.add(self.public_url.rstrip("/"))
        return out

    @property
    def marketplace_index_url_list(self) -> list[str]:
        return [u.strip() for u in self.marketplace_index_urls.split(",") if u.strip()]


settings = Settings()


def warn_unknown_keys() -> None:
    """Startup guard against silently-ignored config typos. `extra="ignore"` swallows
    any CONDUCTOR_* key that doesn't match a Settings field — a misspelled var (say
    CONDUCTOR_JIRA_JQLL) means the default quietly stays in force. Scan the process
    env and every env file in the chain; warn, never block."""
    log = logging.getLogger(__name__)
    valid = {f"CONDUCTOR_{name.upper()}" for name in Settings.model_fields}

    def file_keys(path: Path) -> list[str]:
        try:
            lines = path.read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k = line.split("=", 1)[0].strip()
                if k.startswith("CONDUCTOR_"):
                    out.append(k)
        return out

    sources: list[tuple[str, list[str]]] = [
        ("environment", [k for k in os.environ if k.startswith("CONDUCTOR_")])
    ]
    sources += [(f, file_keys(Path(f))) for f in _ENV_FILES]
    for src, keys in sources:
        for k in keys:
            if k not in valid:
                log.warning("unknown config key %s in %s — ignored (typo?)", k, src)
