"""Host runner: run argv locally or on a configured remote over ssh.

Split out of terminal.py (which mixed five jobs). A session's `host` says which
machine its tmux+claude live on: None = this machine, else a name from
remote_host_map. Local is the degenerate case — remote just wraps the same argv
in ssh. Consumers: terminal.py (tmux/ttyd ops), agent_state.py (pane polls),
metrics.py (host stats).
"""

from __future__ import annotations

import asyncio
import shlex
import time

from ..config import settings

# ── host runner ──────────────────────────────────────────────────────────────
# A session's `host` says which machine its tmux+claude live on: None = this
# machine (settings.local_host_name, "base"), else a name from remote_host_map
# ("roam" → an ssh target). Local is the degenerate case — remote just wraps the
# same argv in ssh. ttyd always stays local: for a remote session it wraps
# `ssh -t roam tmux …`, so ports/proxy never cross machines and a dropped ssh
# leaves tmux+claude alive on the remote.

_SSH_BASE = [
    "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
    # multiplex the many short utility calls (ls/capture-pane every poll) over one
    # TCP+auth handshake instead of paying it per call
    "-o", "ControlMaster=auto", "-o", "ControlPath=~/.ssh/conductor-%r@%h-%p",
    "-o", "ControlPersist=60",
]


def ssh_target(host: str | None) -> str | None:
    """None/local-name → None (run here); a configured remote name → its ssh
    target; anything else is a config error."""
    if not host or host == settings.local_host_name:
        return None
    target = settings.remote_host_map.get(host)
    if not target:
        raise RuntimeError(f"unknown host '{host}' (configure CONDUCTOR_REMOTE_HOSTS)")
    return target


def _sh_path(p: str) -> str:
    """shlex.quote, except a leading ~ is left to expand to the REMOTE $HOME —
    a quoted ~ never expands, and remote cwds arrive as '~/code/x' strings."""
    if p == "~":
        return '"$HOME"'
    if p.startswith("~/"):
        return '"$HOME"/' + shlex.quote(p[2:])
    return shlex.quote(p)


def remote_argv(target: str, inner: str, *, tty: bool = False) -> list[str]:
    """ssh argv running `inner` (a shell command line) on target. Through a login
    shell (-l) so the user's PATH (brew tmux, ~/.local/bin claude) resolves —
    plain `ssh host cmd` skips .zprofile. tty ⇒ interactive attach under ttyd;
    keepalives make a sleeping laptop drop the ssh instead of wedging it."""
    argv = list(_SSH_BASE)
    if tty:
        argv += ["-t", "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]
    argv += [target, f'exec "$SHELL" -l -c {shlex.quote(inner)}']
    return argv


async def _run(
    argv: list[str], host: str | None = None, input_: bytes | None = None, timeout: float = 8
) -> tuple[int, bytes]:
    """Run argv locally, or on `host` over ssh (argv shell-joined remotely)."""
    target = ssh_target(host)
    if target:
        argv = remote_argv(target, shlex.join(argv))
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if input_ is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(input_), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, b""
    return proc.returncode or 0, out


_REACH: dict[str, tuple[float, bool]] = {}


async def reachable(host: str | None, ttl: float = 20.0) -> bool:
    """Is the host's ssh reachable right now? Cached briefly — pollers ask every
    cycle and an offline laptop shouldn't cost a fresh 2s probe each time."""
    if ssh_target(host) is None:
        return True
    now = time.monotonic()
    hit = _REACH.get(host)  # type: ignore[arg-type]
    if hit and now - hit[0] < ttl:
        return hit[1]
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2",
        settings.remote_host_map[host], "true",  # type: ignore[index]
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        rc = await asyncio.wait_for(proc.wait(), 4)
    except asyncio.TimeoutError:
        proc.kill()
        rc = 124
    _REACH[host] = (now, rc == 0)  # type: ignore[index]
    return rc == 0

