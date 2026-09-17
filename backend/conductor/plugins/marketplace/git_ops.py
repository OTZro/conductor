"""Thin async wrapper around the system ``git`` binary — the marketplace's only way of
touching a plugin repo. Every call goes through :func:`_run` so tests can monkeypatch
ONE function, but per the brief we mostly don't: throwaway local repos (``git init`` +
``git tag`` in ``tmp_path``) exercise the real binary, the same way the updater plugin's
tests exercise real git against the actual checkout.

A "ref" throughout this module is either a tag name (``v1.2.0``) or a branch/sha —
whatever ``git checkout`` accepts. Semver selection lives in :mod:`service`, not here:
this module only shells out and parses ``git`` output into plain data.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

log = logging.getLogger("conductor.plugins.marketplace")

DEFAULT_TIMEOUT = 60


class GitError(RuntimeError):
    """A git subprocess failed or timed out — carries stderr for the caller to surface."""


async def _run(*args: str, cwd: Path | None = None, timeout: float = DEFAULT_TIMEOUT) -> str:
    """Run ``git <args>`` and return stdout, raising :class:`GitError` on a non-zero exit
    or timeout. A timed-out process is killed and reaped — the same lesson the updater
    and plugin-manager already learned: an abandoned ``wait_for`` leaves git running."""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=str(cwd) if cwd else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise GitError(f"git {' '.join(args)} timed out after {timeout}s")
    if proc.returncode != 0:
        raise GitError(err.decode(errors="replace").strip()[:500] or f"git {args[0]} failed")
    return out.decode(errors="replace")


async def remote_tags(url: str) -> list[str]:
    """Every tag name the remote advertises, WITHOUT cloning — ``git ls-remote`` talks to
    the remote directly. Peeled annotated-tag rows (``refs/tags/x^{}``) are collapsed to
    the same tag name; a plain ``set`` dedups them. ``--`` marks the end of options so a
    REPO string an attacker got into an index/custom-repo entry (e.g. starting with a
    dash) can never be parsed as a git option instead of the positional repository."""
    out = await _run("ls-remote", "--tags", "--refs", "--", url)
    tags: set[str] = set()
    for line in out.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        if ref.startswith("refs/tags/"):
            tags.add(ref[len("refs/tags/"):])
    return sorted(tags)


async def default_branch_sha(url: str) -> str | None:
    """The remote's HEAD sha — used for the no-tags fallback (install at HEAD, mark
    ``version="dev"``). Same ``--`` guard as :func:`remote_tags`."""
    out = await _run("ls-remote", "--", url, "HEAD")
    line = out.strip().splitlines()[0] if out.strip() else ""
    sha = line.split("\t", 1)[0].strip()
    return sha or None


async def clone(url: str, dest: Path, *, ref: str | None = None) -> None:
    """Shallow clone URL into DEST (removed first if present). REF, when given, is
    passed as ``--branch`` — git accepts a tag name there just as well as a branch, and
    ``--depth 1`` still works for a tag (git resolves it before the shallow fetch).
    ``--`` precedes URL/DEST so neither can be parsed as an option — URL in particular
    is attacker-reachable (an index or custom-repo entry) and git option-injection via
    a leading-dash positional argument is a well-known class of bug for CLI wrappers
    exactly like this one."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["clone", "--depth", "1"]
    if ref:
        args += ["--branch", ref]
    args += ["--", url, str(dest)]
    await _run(*args, timeout=180)


async def fetch_tags(repo_dir: Path) -> None:
    """Refresh REPO_DIR's tag list from its origin — the update path's first step.
    ``--force`` so a moved/re-created tag on the remote (rare, but not our problem to
    police) doesn't make fetch fail."""
    await _run("fetch", "--tags", "--force", "--depth", "1", "origin", cwd=repo_dir, timeout=120)


async def fetch_default_head(repo_dir: Path) -> None:
    """Shallow-fetch the remote's default branch tip into ``FETCH_HEAD`` — the no-tags
    update path. Works without knowing the branch's name (``main`` vs ``master`` vs
    anything else): ``origin HEAD`` is git's own way of saying "whatever the remote's
    HEAD points at right now"."""
    await _run("fetch", "--depth", "1", "origin", "HEAD", cwd=repo_dir, timeout=120)


async def pull_ff_only(repo_dir: Path) -> None:
    """Fast-forward REPO_DIR from its origin — the index-refresh path. ``--ff-only``
    means a diverged/rewritten remote history fails loudly instead of silently
    fabricating a merge commit in what is meant to be a read-only mirror."""
    await _run("pull", "--ff-only", cwd=repo_dir, timeout=120)


async def checkout(repo_dir: Path, ref: str) -> None:
    """Move REPO_DIR's working tree to REF (a tag/branch/sha already fetched)."""
    await _run("checkout", "--force", ref, cwd=repo_dir)


async def local_tags(repo_dir: Path) -> list[str]:
    """Tags known to the local clone (post-fetch) — cheap, no network."""
    out = await _run("tag", "--list", cwd=repo_dir)
    return sorted(t for t in out.splitlines() if t.strip())
