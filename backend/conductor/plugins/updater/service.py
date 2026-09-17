"""Git-level update state for the Conductor checkout, plus the one-click apply.

Everything runs against the repo the backend is checked out from (parents[4] of this
file). The status is cached module-side: the poll (`refresh`) does the network `git
fetch` on a slow cadence and the GET just reads the cache, so opening the header widget
never blocks on the network. Apply shells out to `bin/conductorctl update` detached, so
it survives the very backend restart it triggers.
"""

from __future__ import annotations

import asyncio
import datetime
import time
from pathlib import Path

# backend/conductor/plugins/updater/service.py → the repo root is four parents up.
_ROOT = Path(__file__).resolve().parents[4]
_CTL = _ROOT / "bin" / "conductorctl"
_LOG = Path.home() / ".conductor" / "logs" / "update.log"

# Cached status, served to the widget. `checked_at is None` until the first compute.
_state: dict = {
    "current": None,
    "branch": None,
    "behind": 0,
    "ahead": 0,
    "commits": [],
    "dirty": False,
    "has_upstream": False,
    "updatable": False,
    "blocked_reason": None,
    "fetch_error": False,
    "git_error": False,
    "checked_at": None,
    "applying": False,
    "applying_since": None,  # monotonic stamp — see _claim_is_live
}
_lock = asyncio.Lock()  # one git operation at a time — fetch, compute and apply don't overlap
# Backstop for the `applying` claim if the updater's exit is never observed (the watcher task
# dies with the backend). The widget gives up at 180s; past this the claim is stale, not live.
_APPLY_CLAIM_TTL_S = 300.0
_apply_watch: asyncio.Task | None = None  # kept referenced so the loop can't GC it mid-wait


async def _git(*args: str, timeout: float = 30) -> tuple[int, str]:
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(_ROOT), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, OSError):
        # wait_for cancels the await but leaves git running — kill and reap it so a
        # timed-out fetch doesn't leak a process while the lock is held.
        if proc is not None:
            try:
                proc.kill()
                await proc.communicate()
            except (ProcessLookupError, OSError):
                pass
        return 1, ""
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def _compute() -> dict:
    """Read where HEAD sits relative to its upstream, using whatever the last fetch left
    in the remote-tracking ref (no network here — refresh() owns the fetch)."""
    _, branch = await _git("rev-parse", "--abbrev-ref", "HEAD")
    _, sha = await _git("rev-parse", "--short", "HEAD")
    rc_u, _up = await _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    has_upstream = rc_u == 0 and bool(_up)
    rc_d, dirty_out = await _git("status", "--porcelain", "--untracked-files=no")
    dirty = bool(dirty_out.strip())
    # A git that failed or timed out comes back (1, ""), which is indistinguishable from a
    # clean tree / no incoming commits unless the rc is checked. Reading it as the happy
    # answer would show a confident "you're on the latest" over no information at all, so
    # any nonzero rc makes the whole status unknown rather than clean-and-current.
    unknown = rc_d != 0

    commits: list[dict] = []
    ahead = 0
    if has_upstream:
        rc_l, log = await _git("log", "--format=%h%x09%s", "HEAD..@{u}")
        if rc_l != 0:
            unknown = True
        else:
            for line in log.splitlines():  # newest first
                h, _, subject = line.partition("\t")
                if h:
                    commits.append({"sha": h, "subject": subject})
        rc_a, ahead_out = await _git("rev-list", "--count", "@{u}..HEAD")
        if rc_a == 0 and ahead_out.isdigit():
            ahead = int(ahead_out)
        else:
            unknown = True

    behind = len(commits)
    diverged = behind > 0 and ahead > 0  # both sides moved → --ff-only can't apply it
    reason = (
        "couldn't read the checkout state — git failed or timed out" if unknown
        else "no upstream branch to update from" if not has_upstream
        else "local commits diverge from upstream — resolve by hand" if diverged
        else "local changes in tracked files — commit or stash first" if dirty and behind
        else None
    )
    return {
        "current": sha or None,
        "branch": branch or None,
        "behind": behind,
        "ahead": ahead,
        "commits": commits,
        "dirty": dirty,
        "has_upstream": has_upstream,
        "updatable": has_upstream and behind > 0 and not dirty and not diverged and not unknown,
        "blocked_reason": reason,
        "git_error": unknown,
        "checked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }


async def refresh() -> dict:
    """The poll body (and /check): fetch the remote, recompute, cache. A failed fetch is
    an ERROR — it must not advance the status as if the checkout were confirmed current;
    flag it and keep the last good status/timestamp, and raise so the poller records the
    failure (the /check handler catches it and hands back the cached status)."""
    async with _lock:
        rc, _ = await _git("fetch", "--quiet", timeout=45)
        if rc != 0:
            _state["fetch_error"] = True
            raise RuntimeError("git fetch failed — remote unreachable or auth expired")
        st = await _compute()
    st["fetch_error"] = False
    _state.update(st)
    return dict(_state)


async def current() -> dict:
    """Cached status for the widget. On the very first call (before the poll has run) do a
    no-network compute so the badge is right immediately; the poll refreshes it later."""
    if _state["checked_at"] is None:
        async with _lock:
            st = await _compute()
        _state.update(st)
    return dict(_state)


def _claim_is_live() -> bool:
    """Is the `applying` claim backed by an updater that could still be running? A claim with
    no stamp (or one older than the TTL) is a leftover, not an update in flight."""
    if not _state["applying"]:
        return False
    since = _state["applying_since"]
    return since is None or (time.monotonic() - since) < _APPLY_CLAIM_TTL_S


def _release_claim() -> None:
    _state["applying"] = False
    _state["applying_since"] = None


async def _release_when_updater_exits(proc: asyncio.subprocess.Process) -> None:
    """Drop the claim as soon as `conductorctl update` exits. Every one of its failure paths —
    no upstream, dirty tree, pull/uv sync/npm ci failure, and the already-up-to-date no-op —
    exits BEFORE the restart, so without this nothing would ever clear `applying` and a single
    failure would wedge the button until someone restarted the backend by hand. A success does
    not reach here: the restart it triggers kills this backend (and this task) first."""
    try:
        await proc.wait()
    except (OSError, asyncio.CancelledError):
        pass
    _release_claim()


async def apply() -> dict:
    """Fast-forward + restart, launched DETACHED so it outlives the restart it triggers.
    Single-flight: the whole admission — re-checking the guard and claiming `applying` —
    happens under `_lock` before any process is spawned, so two concurrent /apply calls
    can't both launch competing pulls/restarts. conductorctl re-checks the same guard, so
    a stale cache can't force an unsafe pull either."""
    global _apply_watch
    async with _lock:
        if _claim_is_live():
            return {"started": False, "reason": "an update is already in progress"}
        st = await _compute()
        _state.update(st)
        if not st["updatable"]:
            return {"started": False, "reason": st["blocked_reason"] or "already up to date"}
        _state["applying"] = True  # claimed under the lock, before the spawn
        _state["applying_since"] = time.monotonic()

    _LOG.parent.mkdir(parents=True, exist_ok=True)
    logf = _LOG.open("a")
    try:
        proc = await asyncio.create_subprocess_exec(
            str(_CTL), "update",
            stdout=logf, stderr=logf,
            start_new_session=True,  # its own process group → the backend restart won't kill it
        )
    except OSError as exc:
        _release_claim()  # never got off the ground — let the button re-arm
        return {"started": False, "reason": f"couldn't launch the updater: {exc}"}
    finally:
        logf.close()
    _apply_watch = asyncio.create_task(_release_when_updater_exits(proc))
    # No log path in the response: it is under $HOME, so returning it would hand the OS
    # username to any caller. The widget names ~/.conductor/logs/update.log itself.
    return {"started": True, "from": st["current"]}
