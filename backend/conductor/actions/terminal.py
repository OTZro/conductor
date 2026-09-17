from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import socket
import stat
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from ..config import settings
# split modules — names re-exported so existing imports (tests, metrics, main)
# keep working: terminal is the façade for the terminal subsystem.
from .agent_state import (  # noqa: F401
    _AGENT_RUNNING_RE,
    _BG_COUNT_RE,
    _PANE_SEEN,
    _bg_detail,
    _classify_pane,
    poll_agent_states,
    session_status,
)
from .runner import (  # noqa: F401
    _run,
    _sh_path,
    reachable,
    remote_argv,
    ssh_target,
)

# session_id -> {proc, port, tmux_session, kind, host}
_SESSIONS: dict[str, dict] = {}
_HOOK_DIR = Path("/tmp/conductor-hooks")


def _ensure_hook_dir() -> None:
    """Create the LOCAL hook-settings dir safely. It sits under a world-writable /tmp, and
    claude executes the curl commands inside each settings file we write there — so a
    same-host account that pre-creates (or symlinks) the dir could hijack those commands.
    Refuse a pre-existing symlinked / foreign-owned path, and own it 0700."""
    d = _HOOK_DIR
    if d.is_symlink():
        raise RuntimeError(f"refusing hook dir {d}: it is a symlink")
    if d.exists():
        st = d.stat()
        if not stat.S_ISDIR(st.st_mode):
            raise RuntimeError(f"refusing hook dir {d}: not a directory")
        if st.st_uid != os.getuid():
            raise RuntimeError(f"refusing hook dir {d}: owned by uid {st.st_uid}, not us")
        os.chmod(d, 0o700)
    else:
        d.mkdir(parents=True, mode=0o700)
        os.chmod(d, 0o700)  # mkdir's mode is umask-masked; force 0700


def _hook_cfg(card_id: str, api_base: str, host: str | None = None) -> dict:
    """Claude settings whose hooks report the agent's state back to Conductor.

    State hooks only ever say "working" (instant AI Working on prompt/tool use) or
    "idle" (session ended). "waiting" is deliberately NOT hook-driven: Stop lies
    when a background task is still running (turn ends, bg watcher continues → the
    card would bounce). The 5s tmux pane poll is the single writer of waiting.

    Notification is separate: it fires when claude is blocked on the human
    (permission / question / idle nudge) — the same event that pushes the mobile
    app. We forward its raw payload to /notify and just show the `message` on the
    card. It writes a distinct `notification` field, never the running/waiting
    state, so it can't reintroduce the bounce.

    On top of those built-in state hooks, every plugin's declared ``HookSpec``s are
    folded in generically (see ``_plugin_hooks``): a plugin observes claude activity
    by declaring a hook, needing no edit here — that's the whole point of the plugin
    hook framework. ``host`` (the machine the session runs on) rides along to each
    plugin endpoint so it can read a file locally or over ssh."""
    base = f"{api_base}/api/cards/{card_id}"
    # these endpoints are session-exempt in AuthMiddleware (bare curl, no cookie);
    # when CONDUCTOR_INGEST_TOKEN is set they demand it as a Bearer instead. shlex-quote
    # the header — a token containing a quote must not silently break the curl (the
    # exact silent-hook-failure class this file just got bitten by).
    tok = (
        f" -H {shlex.quote(f'Authorization: Bearer {settings.ingest_token}')}"
        if settings.ingest_token
        else ""
    )

    def hook(state: str) -> dict:
        cmd = (
            f"curl -s -m2 -X POST {base}/agent -H 'Content-Type: application/json'{tok} "
            f"-d '{{\"state\":\"{state}\"}}'"
        )
        return {"hooks": [{"type": "command", "command": cmd}]}

    # forward the hook's stdin JSON ({"message": "...", ...}) verbatim; the backend
    # pulls out `message`. --data-binary @- streams stdin so we needn't parse here.
    notify = f"curl -s -m2 -X POST {base}/notify -H 'Content-Type: application/json'{tok} --data-binary @-"

    hooks: dict[str, list[dict]] = {
        "UserPromptSubmit": [hook("working")],
        "PreToolUse": [hook("working")],
        "PostToolUse": [hook("working")],
        "Notification": [{"hooks": [{"type": "command", "command": notify}]}],
        "SessionEnd": [hook("idle")],
    }
    for event, entry in _plugin_hooks(api_base, card_id, host, tok):
        hooks.setdefault(event, []).append(entry)
    return {"hooks": hooks}


def _plugin_hooks(api_base: str, card_id: str, host: str | None, tok: str):
    """Yield ``(event, hook-entry)`` for every ``HookSpec`` any discovered plugin
    declares, so ``_hook_cfg`` can fold plugin hooks into the generated settings
    without knowing any plugin by name. Each entry forwards the claude hook's stdin
    payload verbatim (``--data-binary @-``) to the plugin's own Conductor route,
    tagging ``?card_id=&host=`` so the plugin knows which card/machine it's for.
    Imported lazily — the plugins package imports this module's siblings, so a
    top-level import would cycle."""
    from ..plugins import runtime

    host_q = f"&host={quote(host, safe='')}" if host else ""
    for _plugin_id, spec in runtime.spec_rows("hooks"):
        url = f"{api_base}{spec.path}?card_id={quote(card_id, safe='')}{host_q}"
        cmd = (
            f"curl -s -m10 -X POST {shlex.quote(url)} "
            f"-H 'Content-Type: application/json'{tok} --data-binary @-"
        )
        entry: dict = {"hooks": [{"type": "command", "command": cmd}]}
        if spec.matcher:
            entry["matcher"] = spec.matcher
        yield spec.event, entry


async def _write_hook_settings(card_id: str, host: str | None = None) -> str | None:
    """Per-card Claude settings file, passed via `claude --settings <file>` so we
    never touch the repo's own .claude config. Local sessions post to 127.0.0.1; a
    REMOTE session's file is pushed over ssh (claude reads it there) and posts to
    public_url — 127.0.0.1 on the remote is the remote itself. Without public_url
    remote sessions run hook-less and the tmux pane poll owns lane state. The push
    doubles as the reachability gate: an offline host fails the open right here."""
    path = _HOOK_DIR / f"{card_id}.json"
    if ssh_target(host) is None:
        # local session: plugin hooks read files on THIS box, so pass host=None
        # (an ssh_target-None host name would otherwise make a plugin ssh to itself).
        _ensure_hook_dir()
        cfg = _hook_cfg(card_id, f"http://127.0.0.1:{settings.port}", host=None)
        path.write_text(json.dumps(cfg))
        return str(path)
    if not settings.public_url:
        return None
    cfg = _hook_cfg(card_id, settings.public_url.rstrip("/"), host=host)
    # harden the remote dir too (0700) — same world-writable-/tmp reasoning as local
    hd = shlex.quote(str(_HOOK_DIR))
    script = f"mkdir -p {hd} && chmod 700 {hd} && cat > {shlex.quote(str(path))}"
    rc, _ = await _run(["sh", "-c", script], host=host, input_=json.dumps(cfg).encode())
    if rc != 0:
        raise RuntimeError(f"host '{host}' unreachable (couldn't push hook settings)")
    return str(path)


def _free_port(start: int, span: int = 300) -> int:
    for p in range(start, start + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    raise RuntimeError("no free port for ttyd")


async def _tmux_has_session(
    name: str, host: str | None = None, socket: str | None = None
) -> bool:
    """Is `name` a LIVE tmux session on `host` (default: local)? `socket` checks a
    non-default `-L <socket>` tmux server — e.g. an agent-team plugin's members run on their own
    named socket, invisible to a plain `tmux has-session`/`tmux ls` (see
    `_build_command`'s generalized watch/attach path below)."""
    argv = [settings.tmux_bin]
    if socket:
        argv += ["-L", socket]
    argv += ["has-session", "-t", name]
    rc, _ = await _run(argv, host=host)
    return rc == 0


def workspace_for(origin: str, external_id: str) -> Path:
    """Where a card's work happens, absent an explicit override. No plugin ships a
    per-card workspace convention today, so this just falls through to the configured
    default_workspace_root (or $HOME) — kept as its own function so a future plugin can
    reintroduce per-origin/external_id workspace resolution without touching callers."""
    if settings.default_workspace_root:
        root = Path(settings.default_workspace_root).expanduser()
        if root.is_dir():
            return root
    return Path.home()


def resolve_cwd(origin: str, external_id: str, override: str | None = None) -> Path:
    """Where to run claude: explicit override → configured default_workspace_root →
    $HOME."""
    if override:
        p = Path(override).expanduser()
        if p.is_dir():
            return p
    return workspace_for(origin, external_id)


def default_cwd(origin: str, external_id: str) -> str:
    """resolve_cwd rendered for the New-session field to PRE-FILL: the dir a fresh
    session would use (CONDUCTOR_DEFAULT_WORKSPACE_ROOT → $HOME), home collapsed back
    to ~ so it stays host-relative. Blanking the field falls back to exactly this on
    the backend — so the field reflects real config, not a hardcoded guess."""
    p = str(resolve_cwd(origin, external_id))
    home = str(Path.home())
    if p == home:
        return "~"
    if p.startswith(home + "/"):
        return "~" + p[len(home):]
    return p


async def list_dirs(path: str, host: str | None = None) -> dict:
    """Directory existence + completions for the New-session working-dir field, checked
    on `host` (local, or remote over ssh). `path` is the partial input, kept verbatim so
    a ~-rooted input yields ~-rooted suggestions (matching what the user typed, and what
    the browser's <datalist> needs to filter). Returns {valid: does `path` name a dir on
    that host, dirs: up to 25 subdir completions of it}. Injection-safe: `path` rides
    through _sh_path (shlex-quoted; a stray `;rm` becomes a filename, not a command)."""
    raw = (path or "").strip()
    slash = raw.rfind("/")
    parent_typed = raw[: slash + 1] if slash >= 0 else ""  # verbatim, incl trailing '/'
    partial = raw[slash + 1 :] if slash >= 0 else raw
    parent = parent_typed.rstrip("/")  # keep '/' itself for absolute roots (/tmp, /u…)
    parent_sh = _sh_path(parent or ("/" if parent_typed.startswith("/") else "~"))
    # one round-trip: is `raw` itself a dir (V), then the parent's entries (ls -F marks
    # dirs with a trailing '/'). 2>/dev/null so a missing parent is just "no completions".
    # head caps the listing shell-side so a pathological huge dir can't be buffered whole
    # before we truncate to 25 (500 leaves ample room to prefix-filter down to 25).
    script = (
        f'test -d {_sh_path(raw or "~")} && printf "V\\n"; '
        f'ls -1F -- {parent_sh} 2>/dev/null | head -500'
    )
    rc, out = await _run(["sh", "-c", script], host=host)
    lines = out.decode(errors="replace").splitlines()
    valid = bool(lines) and lines[0] == "V"
    names = sorted(n[:-1] for n in lines if n.endswith("/"))
    if partial:
        low = partial.lower()
        names = [n for n in names if n.lower().startswith(low)]
    return {"valid": valid, "dirs": [f"{parent_typed}{n}" for n in names[:25]]}


async def _conversation_exists(sid: str, host: str | None = None) -> bool:
    """Does this conversation's transcript exist on the host? Ground truth for
    where a conversation can resume — transcripts never move across machines."""
    rc, out = await _run(
        ["sh", "-c", f"find ~/.claude/projects -maxdepth 2 -name {shlex.quote(sid + '.jsonl')} 2>/dev/null | head -1"],
        host=host,
    )
    return rc == 0 and bool(out.strip())


async def _conversation_cwd(sid: str, host: str | None = None) -> str | None:
    """The directory a conversation was created in, read from its transcript's own
    `cwd` field. `claude --resume` resolves a session by cwd (transcripts are
    namespaced under ~/.claude/projects/<encoded-cwd>/), so it must be launched
    there or it reports "No conversation found" even though the file exists — which
    is exactly what bit PROJ-6595, whose conversation was created in the FMS repo but
    whose resume launched from the default workspace / remote_workspace_root."""
    find = f"find ~/.claude/projects -maxdepth 2 -name {shlex.quote(sid + '.jsonl')} 2>/dev/null | head -1"
    script = "f=$(" + find + "); test -n \"$f\" && grep -m1 -oE '\"cwd\":\"[^\"]*\"' \"$f\""
    rc, out = await _run(["sh", "-c", script], host=host)
    frag = out.decode(errors="replace").strip()
    if rc != 0 or not frag:
        return None
    try:
        cwd = json.loads("{" + frag + "}").get("cwd")
    except ValueError:
        return None
    return cwd if isinstance(cwd, str) and cwd else None


def _remote_cwd(override: str | None) -> str:
    """Working dir on a remote host — its own filesystem, so no local validation.
    Bare relative paths are taken as home-relative (tmux -c wants absolute)."""
    cwd = (override or "").strip() or settings.remote_workspace_root
    if not cwd.startswith(("~", "/")):
        cwd = f"~/{cwd}"
    return cwd


async def _build_command(
    kind: str,
    origin: str,
    external_id: str,
    session_id: str,
    card_id: str,
    cwd_override: str | None,
    claude_session_id: str | None,
    initial_prompt: str | None = None,
    host: str | None = None,
    attach_only: bool = False,
    env: dict | None = None,
    watch: dict | None = None,
) -> tuple[list[str], bool, str | None, str | None, str | None]:
    """Return (argv, writable, tmux_session, cwd, claude_session_id). host only
    applies to own/resume — attach is always local (the API enforces it).
    attach_only (resume path): attach a RUNNING session or fail — never revive a
    killed one (see the branch).

    `kind == "attach"` requires a plugin-declared `cached.watch` naming a live tmux
    (``{"session": str, "socket": str|None, "host": str|None, "writable": bool}`` —
    see api/terminals.py): a card is auto-attachable when some plugin (e.g. an
    agent-team plugin) writes this, exposing an agent member's claude pane on its own
    named socket. writable DEFAULTS True (read-write); the declarer sets it False for
    a read-only view (agent-team members do). No shipped plugin declares one today —
    Watch simply has nothing to attach to until one does."""
    if kind == "attach":
        if not (watch and watch.get("session")):
            raise RuntimeError("card has no cached.watch session to attach")
        w_name = str(watch["session"])
        w_socket = watch.get("socket") or None
        w_host = watch.get("host") or None
        # Writability: the watch declares its wish, the OPERATOR's per-socket policy
        # (CONDUCTOR_EXTRA_TMUX_SOCKETS `<name>:rw`) is the ceiling. A named socket
        # is another daemon's server; a plugin writing cached.watch must not be able
        # to re-open writable what the operator configured (or left, read-only being
        # the unconfigured default) as look-don't-touch. The watch can still be
        # STRICTER (writable=False on a `:rw` socket). Default-socket watches keep
        # the declarer's call — those sessions are conductor's own.
        # Read-only means tmux `-r` AND withholding ttyd's --writable (the returned
        # flag) — a double lock; a writable watch does neither.
        w_writable = bool(watch.get("writable", True))
        if w_socket and settings.extra_tmux_socket_map.get(w_socket, True):
            w_writable = False
        if not await _tmux_has_session(w_name, w_host, w_socket):
            raise RuntimeError(f"no live tmux session '{w_name}' to watch")
        pre = ["tmux"]
        if w_socket:
            pre += ["-L", w_socket]
        pre += ["attach", "-t", w_name]
        if not w_writable:
            # `-r` blocks input at the tmux layer (not merely by withholding ttyd
            # --writable), and `ignore-size` keeps this viewer out of window-size
            # negotiation — without it, just OPENING a read-only watch resizes the
            # watched window and reflows that agent's TUI mid-turn.
            pre += ["-r", "-f", "read-only,ignore-size"]
        w_target = ssh_target(w_host)
        argv = remote_argv(w_target, shlex.join(pre), tty=True) if w_target else pre
        return (argv, w_writable, w_name, None, None)

    target = ssh_target(host)
    cwd = _remote_cwd(cwd_override) if target else str(resolve_cwd(origin, external_id, cwd_override))
    hooks = await _write_hook_settings(card_id, host) if card_id else None

    def claude_argv(*flags: str) -> list[str]:
        argv = [settings.claude_bin]
        if hooks:
            argv += ["--settings", hooks]
        argv += list(flags)
        if initial_prompt:
            argv.append(initial_prompt)  # seed the interactive session's first message
        return argv

    # A launch profile's env must reach the PANE, not the tmux client: an already-running
    # tmux server hands new panes ITS environment (+ update-environment allowlist), NOT
    # the client's — so `env=` on the client/ttyd is silently dropped whenever the server
    # is already up (the common case). Wrap the pane command itself with `env K=V …`
    # instead; env(1) sets the vars for claude directly, server state irrelevant.
    def env_argv() -> list[str]:  # local: values expanded now (no shell runs them)
        if not env:
            return []
        exp = {k: os.path.expanduser(os.path.expandvars(str(v))) for k, v in env.items()}
        return ["env", *[f"{k}={v}" for k, v in exp.items()]]

    def env_prefix_remote() -> str:  # remote: keep ~ for the REMOTE shell to expand
        if not env:
            return ""
        return "env " + " ".join(f"{k}={_sh_path(str(v))}" for k, v in env.items())

    def tmux_cmd(name: str, *claude_flags: str) -> list[str]:
        pre = ["tmux", "new-session", "-A", "-s", name, "-c"]
        post = claude_argv(*claude_flags)
        if target:
            # one shell line for the remote side; the cwd piece keeps its leading ~
            # unquoted so it expands to the REMOTE home
            inner = " ".join(
                x
                for x in [shlex.join(pre), _sh_path(cwd), env_prefix_remote(), shlex.join(post)]
                if x
            )
            return remote_argv(target, inner, tty=True)
        return [*pre, cwd, *env_argv(), *post]

    def tmux_attach(name: str) -> list[str]:
        # pure attach: unlike `new-session -A` it can NEVER create, so a killed
        # session stays killed. host-aware + writable (it's our own session), unlike
        # the read-only `attach` kind above. Literal "tmux" (remote PATH), as
        # tmux_cmd does — settings.tmux_bin is a local path that won't exist on a peer.
        pre = ["tmux", "attach", "-t", name]
        return remote_argv(target, shlex.join(pre), tty=True) if target else pre

    # own (fresh, generate a session id) / resume (continue a known one)
    if kind == "resume":
        if not claude_session_id:
            raise RuntimeError("resume needs a claude_session_id")
        sid, flag = claude_session_id, "--resume"
        name = f"conductor-{sid[:8]}"
        # auto-open on card view (attach_only): ATTACH a running session, never REVIVE
        # a killed one. A dead session here means "nothing to attach" — fail so the
        # caller falls through to its live-session scan, instead of tmux_cmd's
        # new-session -A silently recreating (resurrecting) it. Reviving a past
        # conversation is the explicit Resume button's job, not card-open's.
        if attach_only:
            if not await _tmux_has_session(name, host):
                raise RuntimeError(f"conversation {sid[:8]} has no live tmux to attach")
            return (tmux_attach(name), True, name, None, sid)
        # a conversation only resumes where it physically lives: accept when its
        # tmux is still up on that host (attach) or its transcript is there;
        # otherwise fail loudly instead of spawning a claude that errors out.
        if not (await _tmux_has_session(name, host) or await _conversation_exists(sid, host)):
            where = host or settings.local_host_name
            raise RuntimeError(
                f"conversation {sid[:8]} not found on {where} — it lives on another "
                "machine or its transcript was cleaned up"
            )
        # claude resolves --resume by cwd; launching it anywhere but the dir the
        # conversation was created in makes it print "No conversation found" even
        # though the transcript is right there. Pin cwd to the transcript's own
        # recorded cwd (per-host), overriding resolve_cwd/_remote_cwd. (PROJ-6595)
        conv_cwd = await _conversation_cwd(sid, host)
        if conv_cwd and (target or Path(conv_cwd).is_dir()):
            cwd = _remote_cwd(conv_cwd) if target else conv_cwd
        return (tmux_cmd(name, flag, sid), True, name, cwd, sid)
    sid, flag = (claude_session_id or str(uuid.uuid4())), "--session-id"  # own
    name = f"conductor-{sid[:8]}"
    return (tmux_cmd(name, flag, sid), True, name, cwd, sid)


async def open_terminal(
    *,
    session_id: str,
    origin: str,
    external_id: str,
    kind: str,
    card_id: str = "",
    cwd: str | None = None,
    claude_session_id: str | None = None,
    font_size: int | None = None,
    initial_prompt: str | None = None,
    host: str | None = None,
    attach_only: bool = False,
    env: dict | None = None,
    watch: dict | None = None,
    term_theme: dict | None = None,  # xterm palette for this viewer (see _theme_args)
    tmux_style: dict | None = None,  # theme's tmux styles (see apply_tmux_style)
) -> dict:
    command, writable, tmux_session, used_cwd, used_sid = await _build_command(
        kind, origin, external_id, session_id, card_id, cwd, claude_session_id,
        initial_prompt, host, attach_only, env, watch,
    )
    port = _free_port(settings.ttyd_port_start)
    fs = font_size or settings.terminal_font_size
    # ttyd binds loopback under a base path so the backend can reverse-proxy it at
    # /term/<id>/ (same origin → works over tailscale serve). disableLeaveAlert
    # kills ttyd's "leave site?" prompt on reload/switch. ttyd is ALWAYS local —
    # for a remote host the command it wraps is the ssh itself.
    args = [settings.ttyd_bin, "-p", str(port), "-i", "127.0.0.1",
            "-b", f"/term/{session_id}", "-t", f"fontSize={fs}",
            "-t", "disableLeaveAlert=true",
            # ⌥Option+drag forces a local selection even when tmux mouse mode is on,
            # so text can be copied to the browser/Mac clipboard (Shift+drag also works).
            "-t", "macOptionClickForcesSelection=true"]
    args += _theme_args(term_theme)
    if writable:
        args.append("--writable")
    args += command
    # profile env rides the pane command itself (`env K=V claude …`, built in
    # _build_command) — NOT the ttyd/tmux-client environment, which a running tmux server
    # ignores. So ttyd just inherits Conductor's env unchanged.
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    if tmux_session:
        # a cached.watch session lives on ITS socket; styling must look there, or the
        # ownership check (and any set-option) runs against whatever session happens to
        # carry the same name on the DEFAULT server (open_raw_tmux already passes its
        # socket through — this call site now agrees)
        w_sock = (watch or {}).get("socket") or None if kind == "attach" else None
        await apply_tmux_style(tmux_session, host, tmux_style, socket=w_sock)
    _SESSIONS[session_id] = {
        "proc": proc, "port": port, "tmux_session": tmux_session, "kind": kind, "host": host,
    }
    return {
        "port": port,
        "url": f"/term/{session_id}/",
        "tmux_session": tmux_session,
        "pid": proc.pid,
        "cwd": used_cwd,
        "claude_session_id": used_sid,
        "host": host,
    }


async def stop_terminal(session_id: str) -> bool:
    info = _SESSIONS.pop(session_id, None)
    if not info:
        return False
    try:
        info["proc"].terminate()
    except ProcessLookupError:
        pass
    return True


def is_alive(session_id: str) -> bool:
    info = _SESSIONS.get(session_id)
    return bool(info and info["proc"].returncode is None)


def port_for(session_id: str) -> int | None:
    info = _SESSIONS.get(session_id)
    return info["port"] if info else None


def session_tmux(session_id: str) -> str | None:
    info = _SESSIONS.get(session_id)
    return info["tmux_session"] if info else None


def session_host(session_id: str) -> str | None:
    info = _SESSIONS.get(session_id)
    return info.get("host") if info else None


async def kill_tmux(name: str, host: str | None = None) -> None:
    """Terminate the tmux session (and the claude inside it), plus any ttyd bound to it.
    ttyd runs as its own process holding a port; killing only the tmux leaves ttyd alive
    with a leaked port (see reap_orphan_ttyd for the cross-restart case)."""
    for sid in [
        s for s, info in list(_SESSIONS.items())
        if info.get("tmux_session") == name and info.get("host") == host
    ]:
        await stop_terminal(sid)
    await _run([settings.tmux_bin, "kill-session", "-t", name], host=host)
    invalidate_tmux_cache()


async def kill_card_sessions(card_id: str) -> int:
    """Kill a card's Conductor tmux sessions (e.g. when it reaches Done). The claude
    conversation persists on disk, so `claude --resume <sid>` from the UI still works —
    this only frees the live tmux/claude. Sessions on an unreachable remote host are
    skipped (they'll be hit next time the host is back and the card transitions again,
    or reaped)."""
    from sqlalchemy import select  # lazy: avoid an import cycle with store

    from ..db import session_maker
    from ..models import TerminalSession

    targets: set[tuple[str, str | None]] = set()
    async with session_maker() as session:
        rows = (
            await session.execute(select(TerminalSession).where(TerminalSession.card_id == card_id))
        ).scalars().all()
        for r in rows:
            if r.tmux_session and r.tmux_session.startswith("conductor-"):
                targets.add((r.tmux_session, r.host))
    killed = 0
    for name, host in targets:
        if host and not await reachable(host):
            continue
        if await _tmux_has_session(name, host):
            await kill_tmux(name, host)
            killed += 1
    return killed


async def reap_orphan_tmux() -> int:
    """Kill conductor-* tmux sessions that no longer map to any card. Pruning a card
    deletes its TerminalSession rows but never killed the tmux, leaking an idle claude
    per pruned card. Every conductor-* session gets its DB row BEFORE the tmux is
    created, so no-row ⇒ its card/rows were deleted ⇒ orphan. Sessions of still-existing
    cards are left alone even when Done (a live session there is a deliberate resume —
    kill-on-done already handled the transition), as are attached sessions."""
    from sqlalchemy import select  # lazy: avoid an import cycle with store

    from ..db import session_maker
    from ..models import TerminalSession

    async with session_maker() as session:
        rows = (await session.execute(select(TerminalSession))).scalars().all()
    mapped = {(r.host, r.tmux_session) for r in rows if r.tmux_session}
    killed = 0
    for t in await list_tmux():
        name = t.get("name") or ""
        if t.get("kind") != "conductor" or (t.get("host"), name) in mapped or t.get("attached"):
            continue
        await kill_tmux(name, t.get("host"))
        killed += 1
    # also free ttyd whose tmux died outside kill_tmux (manual kill, tmux server bounce) —
    # otherwise the ttyd lingers holding its port until the next backend restart. An
    # UNREACHABLE remote is not "died": its tmux is (probably) alive behind a dead ssh,
    # so leave the ttyd alone until the host answers again.
    for sid in list(_SESSIONS):
        info = _SESSIONS.get(sid)
        tname = info.get("tmux_session") if info else None
        host = info.get("host") if info else None
        if not tname or (host and not await reachable(host)):
            continue
        if not await _tmux_has_session(tname, host):
            await stop_terminal(sid)
    # status truth: rows whose tmux is gone said "live" forever. Mark them ended —
    # skipping rows on unreachable remotes (their tmux is probably alive behind a
    # dead ssh; don't falsely close them).
    live_now = {(t.get("host"), t["name"]) for t in await list_tmux()}
    async with session_maker() as session:
        rows = (await session.execute(select(TerminalSession))).scalars().all()
        dirty = False
        for r in rows:
            if r.status not in ("live", "starting", "stopped") or not r.tmux_session:
                continue
            if r.host and not await reachable(r.host):
                continue
            if (r.host, r.tmux_session) not in live_now:
                r.status = "ended"
                dirty = True
        if dirty:
            await session.commit()
    return killed


async def reap_orphan_ttyd() -> int:
    """Kill ttyd processes left over from a PRIOR backend run. `_SESSIONS` is in-memory,
    so on startup every one of our ttyd is an orphan holding a port that nothing tracks —
    clear them so the 7500+ range doesn't fill up across restarts. Match on the
    `conductor-` tmux session name every one of ours carries (covers both the current
    `-b /term/… tmux attach` form and the older `tmux new-session -s conductor-…` one),
    SIGKILL since orphans need no graceful shutdown. Startup-only, before any of our
    sessions exist."""
    proc = await asyncio.create_subprocess_exec(
        "pkill", "-9", "-f", "ttyd.*conductor-",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode  # pkill: 0 = killed ≥1, 1 = none matched



async def capture_tmux(name: str, host: str | None = None) -> str:
    """Grab the pane's text (visible + a little scrollback) so the browser can put
    it in a selectable box — works around tmux mouse-mode eating drag-to-select."""
    rc, out = await _run([settings.tmux_bin, "capture-pane", "-p", "-S", "-200", "-t", name], host=host)
    return out.decode(errors="replace") if rc == 0 else ""


async def scroll_tmux(
    name: str, direction: str, host: str | None = None, lines: int = 1
) -> dict:
    """Drive tmux copy-mode from the server — the browser can't scroll a claude pane
    itself (xterm has no scrollback of its own; tmux owns the ALT buffer, which keeps
    none). Line-granular like any terminal, and `lines` arrive batched: one gesture
    costs one tmux round trip and ONE pane redraw instead of N, which is what made
    per-page requests feel laggy. tmux clamps at the oldest line on its own.

    Returns the resulting position, so the caller never has to track it and drift.
    Landing at the bottom leaves copy-mode: staying in it would swallow typing as
    copy-mode commands, which reads as a frozen claude.

    A full-screen app (claude v2, vim, less, htop) runs on the pane's ALTERNATE
    screen and owns its OWN scrollback — tmux copy-mode can't move it (the alt screen
    keeps no tmux scrollback; scroll_position stays 0). There we page the app with its
    own PageUp/PageDown instead; copy-mode is only for the NORMAL buffer (a shell
    prompt), where tmux holds the scrollback. `alt` in the result tells the caller which
    regime ran, so it can skip the copy-mode badge/typing-intercept.

    BOTH regimes ship in ONE invocation, each command gated on tmux's own
    `#{alternate_on}`, so nothing here has to ask first and then act on a stale answer:
    the pane cannot leave the alt screen between the question and the keystroke, and a
    gesture still costs a single exec (one ssh round trip on a Roam session). The whole
    list runs under one status, so a failed send is a failed scroll, never a silent one.

    The alt branch pages ONCE per request: `PageUp` is one screen and there is no depth
    reading to correct an overshoot with, so the client's own gesture metering (it
    re-requests while `pending` drains) is what turns a fling into several pages."""
    n = max(1, min(int(lines or 1), 200))
    # Validate BEFORE touching tmux: an unknown direction stays a pure no-op rather than
    # a remote exec that can fail and surface as a 502.
    if direction not in ("up", "down", "exit"):
        return {"in_mode": False, "scroll": 0, "alt": False}

    # one exec, several tmux commands: a bare ';' argv element is tmux's separator
    # (shlex.join quotes it for the remote case, which is exactly right — the remote
    # shell must not eat it)
    # `send-keys -X` FAILS on a pane that isn't in copy-mode ("not in a mode"), and a
    # failing command aborts the rest of the list — including the state read. Same for a
    # copy-mode key aimed at a pane that turned out to be full-screen. Gate every command
    # on tmux's own flags so the list always completes and a nonzero status means only one
    # thing: the command never reached tmux.
    ALT = "#{alternate_on}"  # full-screen app: it owns the screen, page it with its keys
    NORM = "#{?alternate_on,0,1}"  # normal buffer: tmux holds the scrollback
    NORM_IN_MODE = "#{?alternate_on,0,#{pane_in_mode}}"  # …and copy-mode is already open

    def when(cond: str, *cmd: str) -> list[str]:
        return ["if", "-F", "-t", name, cond, shlex.join(list(cmd))]

    argv: list[str] = []

    def add(part: list[str]) -> None:
        argv.extend(([";"] if argv else []) + part)

    if direction in ("up", "down"):
        if settings.alt_scroll_mode == "wheel":
            # A full-screen app that asked for mouse reporting (claude does —
            # `#{mouse_any_flag}` is 1 on every claude pane) scrolls its OWN transcript
            # from wheel events, line by line. That is precisely what native tmux gives
            # you: its default WheelUpPane binding forwards the event with `send-keys -M`
            # rather than translating it, which is why scrolling a claude pane in a plain
            # tmux feels line-granular while PageUp jumps a screen.
            # Sent as HEX so the SGR escape survives shlex.join and, for a remote host,
            # the ssh shell too — a literal ESC through two levels of quoting is a
            # silent-corruption risk this avoids entirely.
            btn = 64 if direction == "up" else 65  # SGR: 64 = wheel up, 65 = wheel down
            one = f"\x1b[<{btn};10;10M".encode("ascii", "ignore")
            # one wheel event per line asked for, bounded: a fling already arrives
            # batched, and past a couple of screens the app clamps anyway.
            hexes = [f"{b:02x}" for b in one * min(n, 30)]
            add(when(ALT, "send-keys", "-t", name, "-H", *hexes))
        else:
            add(when(ALT, "send-keys", "-t", name, "PageUp" if direction == "up" else "PageDown"))
    if direction == "up":
        add(when(NORM, "copy-mode", "-t", name))  # enters, or a no-op when already in it
        # -N repeats the copy-mode command n times in ONE send-keys — a 60-line gesture
        # is one guarded command, not 60 (each of which a full-screen pane would skip).
        add(when(NORM, "send-keys", "-t", name, "-X", "-N", str(n), "scroll-up"))
    elif direction == "down":
        add(when(NORM_IN_MODE, "send-keys", "-t", name, "-X", "-N", str(n), "scroll-down"))
    else:  # exit — unconditional: a pane can be full-screen AND in copy-mode at once, and
        # leaving one stuck there with the badge cleared is the frozen-claude shape above.
        add(when("#{pane_in_mode}", "send-keys", "-t", name, "-X", "cancel"))
    add(["display-message", "-p", "-t", name, "#{alternate_on} #{pane_in_mode} #{scroll_position}"])

    rc, out = await _run([settings.tmux_bin, *argv], host=host)
    parts = out.decode(errors="replace").split()
    if rc != 0 or len(parts) < 2:
        # A failed command — an unreachable remote, a vanished pane — says nothing about
        # the pane. Reporting "live" would be a lie the browser acts on: it clears the
        # badge and stops intercepting input while tmux may still be in copy-mode, and
        # unlike a rejected request nothing would trigger its recovery. Fail loudly so
        # the caller's error path (which assumes copy-mode) is the one that runs.
        raise RuntimeError(f"tmux scroll failed on '{name}' (rc={rc})")
    alt = parts[0] == "1"
    in_mode = parts[1] == "1"
    # scroll_position expands EMPTY outside copy-mode, so the third field can be absent
    at = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    # In copy-mode but not moved (a pane with no scrollback yet, or already at the oldest
    # line) — copy-mode buys nothing there and still swallows typing, while a zero depth
    # is exactly what the browser reads as "live". Leaving keeps `in_mode ⇒ scroll > 0`
    # true, so the two can't disagree.
    if in_mode and at == 0:
        await _run([settings.tmux_bin, "send-keys", "-t", name, "-X", "cancel"], host=host)
        in_mode = False
    return {"in_mode": in_mode, "scroll": at, "alt": alt}


async def paste_image(name: str, host: str | None, data: bytes, ext: str) -> str:
    """Drop a pasted image onto the SESSION's host (Base local, or Roam over ssh —
    claude reads its own machine's filesystem, never the browser's), then type the
    absolute path into the pane so claude can Read it. Returns the path.

    This is how a Roam-clipboard image reaches a Base claude: the browser (on Roam)
    grabs the bytes, uploads here, we land them next to claude and inject the path."""
    fname = f"{uuid.uuid4().hex}.{ext}"
    if ssh_target(host) is None:
        d = Path.home() / ".conductor" / "pasted"
        d.mkdir(parents=True, exist_ok=True)
        dest = d / fname
        dest.write_bytes(data)
        path = str(dest)
    else:
        # write bytes over ssh, then echo back the $HOME-expanded absolute path
        rel = shlex.quote(f".conductor/pasted/{fname}")
        script = (
            f'mkdir -p "$HOME"/.conductor/pasted && cat > "$HOME"/{rel} '
            f'&& printf %s "$HOME"/{rel}'
        )
        rc, out = await _run(["sh", "-c", script], host=host, input_=data, timeout=30)
        if rc != 0 or not out.strip():
            raise RuntimeError(f"couldn't write the image on host '{host}'")
        path = out.decode(errors="replace").strip()
    # -l = literal: send the path as text (no key interpretation); trailing space so
    # the user's own prompt appends cleanly. They add context + Enter to submit.
    await _run([settings.tmux_bin, "send-keys", "-t", name, "-l", f"{path} "], host=host)
    return path


# ── handover: move a task between machines via a handover doc (not the transcript) ──────
_HANDOVER_REL = ".conductor/handover"  # under $HOME on each machine


async def send_prompt(name: str, text: str, host: str | None = None) -> None:
    """Type `text` into a live claude pane and submit it (used to inject the handover
    write-prompt). `-l` sends it literally; a separate Enter submits — so `text` must be a
    SINGLE line (an embedded newline would submit early). Best used while claude is idle."""
    await _run([settings.tmux_bin, "send-keys", "-t", name, "-l", text], host=host)
    await _run([settings.tmux_bin, "send-keys", "-t", name, "Enter"], host=host)


async def read_handover(card_id: str, host: str | None = None) -> str | None:
    """Read a card's handover doc from `host` ($HOME/.conductor/handover/<id>.md), or None
    if it isn't there yet (claude hasn't written it)."""
    rc, out = await _run(
        ["sh", "-c", f'cat "$HOME"/{_HANDOVER_REL}/{card_id}.md 2>/dev/null'], host=host
    )
    txt = out.decode(errors="replace")
    return txt if rc == 0 and txt.strip() else None


async def write_handover(card_id: str, content: str, host: str | None = None) -> None:
    """Write a card's handover doc onto `host` (creating the dir) — how it crosses machines
    (local read + ssh write over the tailnet), so it never touches the repo."""
    script = f'mkdir -p "$HOME"/{_HANDOVER_REL} && cat > "$HOME"/{_HANDOVER_REL}/{card_id}.md'
    rc, _ = await _run(["sh", "-c", script], host=host, input_=content.encode(), timeout=30)
    if rc != 0:
        raise RuntimeError(f"couldn't write the handover on '{host or settings.local_host_name}'")


async def clear_handover(card_id: str, host: str | None = None) -> None:
    """Delete a card's handover doc on `host`. Done before injecting the write-prompt so a
    poll for the doc only succeeds on a FRESH write, never a stale one from a prior hand-off."""
    await _run(["sh", "-c", f'rm -f "$HOME"/{_HANDOVER_REL}/{card_id}.md'], host=host)


# xterm.js ITheme keys we forward. An allowlist, not a passthrough: the value ends up
# in ttyd's argv, and while argv carries no shell-injection risk, an unbounded blob
# would let a caller push arbitrary client options through the `key=value` parser.
_THEME_KEYS = frozenset({
    "background", "foreground", "cursor", "cursorAccent", "selectionBackground",
    "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
    "brightBlack", "brightRed", "brightGreen", "brightYellow",
    "brightBlue", "brightMagenta", "brightCyan", "brightWhite",
})
_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def _theme_args(term_theme: dict | None) -> list[str]:
    """`-t theme={…}` for ttyd, or [] when no (valid) theme was supplied — in which case
    xterm keeps its built-in dark palette, exactly as before this existed. Every key is
    allowlisted and every value must be a plain hex colour; anything else is dropped
    rather than trusted, since this crosses from the browser into a subprocess argv."""
    if not isinstance(term_theme, dict):
        return []
    clean = {
        k: v for k, v in term_theme.items()
        if k in _THEME_KEYS and isinstance(v, str) and _COLOR_RE.match(v)
    }
    return ["-t", "theme=" + json.dumps(clean, separators=(",", ":"))] if clean else []


# ── tmux styling (a theme's status bar / borders) ────────────────────────────────
# Same allowlist as api/themes.py, enforced again here because this is the edge that
# actually reaches a `tmux set-option` argv.
_TMUX_STYLE_OPTS = frozenset({
    "status-style", "status-left-style", "status-right-style",
    "window-status-style", "window-status-current-style",
    "pane-border-style", "pane-active-border-style",
    "message-style", "mode-style",
})
_TMUX_STYLE_VALUE_RE = re.compile(r"^[A-Za-z0-9#,=_ -]{1,200}$")
# Only sessions CONDUCTOR created. tmux styles are session-scoped, not per-client, so
# they are visible to every viewer of that session — including the user's own terminal
# attached from outside. Restyling someone else's agent (an agent member's pane) because
# a conductor tab changed theme would be exactly the kind of reach-across this codebase
# avoids elsewhere; a named socket is by definition another daemon's, so it is excluded
# outright.
_OWNED_PREFIXES = ("conductor-", "shell-")


def owns_tmux_session(name: str, socket: str | None = None) -> bool:
    return socket is None and name.startswith(_OWNED_PREFIXES)


async def apply_tmux_style(
    name: str, host: str | None, styles: dict | None, socket: str | None = None
) -> None:
    """Push a theme's tmux styles onto one of OUR sessions. ``styles`` semantics mirror
    the wire: a populated dict sets those options; an EMPTY dict unsets the allowlisted
    ones (`set -u`), so switching to a theme that styles nothing restores the user's own
    tmux.conf instead of leaving the previous theme's colours stuck; ``None`` means the
    caller expressed no opinion and nothing is touched. Best-effort — a tmux that
    rejects one option must not fail the terminal open."""
    if styles is None or not owns_tmux_session(name, socket):
        return
    argv_base = [settings.tmux_bin]
    if socket:
        argv_base += ["-L", socket]
    if not styles:
        for opt in sorted(_TMUX_STYLE_OPTS):
            await _run([*argv_base, "set-option", "-t", name, "-u", opt], host=host)
        return
    for opt, val in styles.items():
        if opt not in _TMUX_STYLE_OPTS or not isinstance(val, str):
            continue
        if not _TMUX_STYLE_VALUE_RE.match(val):
            continue
        await _run([*argv_base, "set-option", "-t", name, opt, val], host=host)


async def _list_tmux_one(host: str | None, socket: str | None = None) -> list[dict]:
    """Live tmux sessions on one host + one tmux server. `socket` names a non-default
    `-L <socket>` server (e.g. an agent team's `agents`); None = the default socket. A
    named-socket server that isn't running just returns rc!=0 → [] (not an error)."""
    argv = [settings.tmux_bin]
    if socket:
        argv += ["-L", socket]
    argv += ["ls", "-F", "#{session_name}\t#{session_attached}\t#{session_activity}"]
    rc, out = await _run(argv, host=host)
    if rc != 0:
        return []
    sessions = []
    for line in out.decode(errors="replace").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        name = parts[0]
        kind = "conductor" if name.startswith("conductor-") else "other"
        sessions.append(
            {
                "name": name,
                "kind": kind,
                "attached": len(parts) > 1 and parts[1] == "1",
                "activity": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0,
                "host": host,
                "socket": socket,
                "readonly": socket is not None and settings.extra_tmux_socket_map.get(socket, True),
            }
        )
    return sessions


# One tmux listing shared by every caller for a moment. Opening a card fires this from
# several independent effects at once (the drawer, its session list, the auto-attach
# scan), and each miss costs an ssh round-trip per remote host — the burst was measured
# at 8 calls for a single card open. Any mutation WE perform clears it immediately, so
# the window can only ever hide a change made outside conductor.
_TMUX_LS: tuple[float, list[dict]] | None = None


def invalidate_tmux_cache() -> None:
    global _TMUX_LS
    _TMUX_LS = None


async def list_tmux(ttl: float = 2.0) -> list[dict]:
    """All live tmux sessions on every reachable host, categorized (conductor / other).
    host=None entries are local; offline remotes just drop out.

    Named sockets (settings.extra_tmux_socket_list, e.g. `agents`) are swept LOCALLY
    ONLY: they belong to another local daemon, so probing them over ssh bought nothing
    and doubled the per-host round-trips. Everything runs concurrently — the cost is one
    ssh, not one per host×socket in series.
    """
    global _TMUX_LS
    now = time.monotonic()
    if _TMUX_LS is not None and now - _TMUX_LS[0] < ttl:
        return _TMUX_LS[1]
    jobs = [_list_tmux_one(None, None)]
    jobs += [_list_tmux_one(None, sock) for sock in settings.extra_tmux_socket_list]
    for name in settings.remote_host_map:
        if await reachable(name):
            jobs.append(_list_tmux_one(name, None))
    sessions = [s for group in await asyncio.gather(*jobs) for s in group]
    _TMUX_LS = (now, sessions)
    return sessions


async def open_raw_tmux(
    name: str,
    writable: bool = True,
    font_size: int | None = None,
    host: str | None = None,
    socket: str | None = None,
    term_theme: dict | None = None,
    tmux_style: dict | None = None,
) -> dict:
    """Open a ttyd attached to an existing tmux session by name (card-less, for
    the Terminals manager). Lives only in _SESSIONS; the proxy resolves its port.
    `socket` attaches on a non-default `-L <socket>` server (e.g. an agent team's
    `agents`). Whether such a session is read-only (ttyd non-writable + `tmux attach
    -r`) is decided PER socket by settings.extra_tmux_socket_map — a member's pane has an
    automated writer (its daemon pastes + Enter), so read-only is the default; `<socket>:rw`
    in CONDUCTOR_EXTRA_TMUX_SOCKETS attaches writable instead."""
    if socket and settings.extra_tmux_socket_map.get(socket, True):
        writable = False
    sid = uuid.uuid4().hex
    port = _free_port(settings.ttyd_port_start)
    fs = font_size or settings.terminal_font_size
    args = [settings.ttyd_bin, "-p", str(port), "-i", "127.0.0.1",
            "-b", f"/term/{sid}", "-t", f"fontSize={fs}",
            "-t", "disableLeaveAlert=true",
            "-t", "macOptionClickForcesSelection=true"]
    args += _theme_args(term_theme)
    if writable:
        args.append("--writable")
    attach = ["tmux"]
    if socket:
        attach += ["-L", socket]
    attach += ["attach"]
    if not writable:
        # the same hardening as _build_command's attach paths, for the same reason:
        # `-r` blocks input but still joins window-size negotiation, so a bare -r
        # viewer resizes the watched window and reflows that agent's TUI mid-turn.
        # The two paths drifted once already — the argv test pins this one now too.
        attach += ["-r", "-f", "read-only,ignore-size"]
    attach += ["-t", name]
    target = ssh_target(host)
    args += remote_argv(target, f"exec {shlex.join(attach)}", tty=True) if target else attach
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
    )
    await apply_tmux_style(name, host, tmux_style, socket)
    _SESSIONS[sid] = {
        "proc": proc, "port": port, "tmux_session": name, "kind": "raw",
        "host": host, "socket": socket,
    }
    invalidate_tmux_cache()
    return {"id": sid, "url": f"/term/{sid}/", "tmux_session": name, "host": host}


async def new_shell(host: str | None = None, name: str | None = None) -> dict:
    """Create a fresh, card-less shell tmux session on `host` and open a ttyd on it —
    a plain terminal on Base or Roam, unrelated to any card. Named `shell-<6hex>` so
    it lands in the 'other' bucket (never mistaken for a conductor session)."""
    name = name or f"shell-{uuid.uuid4().hex[:6]}"
    rc, _ = await _run([settings.tmux_bin, "new-session", "-d", "-s", name], host=host)
    invalidate_tmux_cache()
    if rc != 0:
        where = host or settings.local_host_name
        raise RuntimeError(f"couldn't create a tmux session on '{where}'")
    return await open_raw_tmux(name, host=host)
