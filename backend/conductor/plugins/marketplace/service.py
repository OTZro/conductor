"""Marketplace core logic: manifest validation, semver tag selection, and the
install/update/remove state machine. Kept apart from ``router.py`` so it can be
exercised directly in tests without going through FastAPI, and apart from
``git_ops.py`` so the git-shelling stays a thin, separately-testable layer.

State lives in two places:
  - ``~/.conductor/marketplace.json`` — ``{"installed": {name: {...}}, "custom_repos":
    [...]}``. Written whole (read-merge-write, same pattern as the core plugin
    switchboard's ``write_plugins_conf``) so one write never clobbers the other key.
  - ``~/.conductor/marketplace/repos/<name>`` — the git clone cache, kept across
    updates (a `fetch` is cheaper than a re-clone) and left behind on remove.
  - ``~/.conductor/marketplace/index/<slug>`` — mirrors of git-sourced index.json
    repos (see :func:`is_http_index_url` / :func:`index_slug`), refreshed via
    ``git pull --ff-only`` at most every 10 minutes.

Every path below is a MODULE-LEVEL constant precisely so tests can monkeypatch it
(``monkeypatch.setattr(service, "_STATE_FILE", tmp_path / "marketplace.json")``) —
the same idiom ``plugins/files/storage.py`` uses for ``FILES_DIR``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path

import httpx

from conductor.models import utcnow

from . import git_ops

log = logging.getLogger("conductor.plugins.marketplace")

# …/backend/conductor/plugins/marketplace/service.py → repo root
_ROOT = Path(__file__).resolve().parents[4]
_BE_LOCAL = _ROOT / "backend" / "conductor" / "plugins" / "local"
_FE_LOCAL = _ROOT / "frontend" / "src" / "plugins" / "local"

_MKT_HOME = Path.home() / ".conductor" / "marketplace"
_REPOS_DIR = _MKT_HOME / "repos"
_TMP_DIR = _MKT_HOME / "tmp"
_INDEX_DIR = _MKT_HOME / "index"  # git-sourced index mirrors, one dir per source (see index_slug)
_STATE_FILE = Path.home() / ".conductor" / "marketplace.json"
_APPLY_LOG = Path.home() / ".conductor" / "logs" / "marketplace-apply.log"

# name convention shared with the install/update/remove guards: lowercase, starts with
# a letter, 2-41 chars total — matches the plugin manager's directory-name discipline.
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,40}$")
# a manifest's declared version must be a git-tag-shaped semver, or the literal "dev"
# stamp a tagless install/update records.
VERSION_RE = re.compile(r"^v?\d+\.\d+\.\d+$")
_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")


class MarketplaceError(Exception):
    """Any user-facing failure — manifest problems, git failures, state conflicts.
    Carries an HTTP status so the router can raise it verbatim without a translation
    table."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# --- semver -----------------------------------------------------------------------


def parse_semver(tag: str) -> tuple[int, int, int] | None:
    """(major, minor, patch) for a ``vX.Y.Z`` / ``X.Y.Z`` tag, or None for anything
    else (pre-releases, build metadata, unrelated tags) — v1 only orders plain triples,
    matching the brief's manifest convention."""
    m = _SEMVER_RE.match(tag.strip())
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


def latest_semver_tag(tags: list[str]) -> str | None:
    """The highest semver-shaped tag in TAGS, or None if none parse — the "no tags"
    case the caller falls back to default-branch HEAD for."""
    parsed = [(v, t) for t in tags if (v := parse_semver(t)) is not None]
    if not parsed:
        return None
    parsed.sort(key=lambda vt: vt[0])
    return parsed[-1][1]


# --- manifest -----------------------------------------------------------------------


def validate_manifest(data: object) -> dict:
    """Raise :class:`MarketplaceError` on any schema violation; otherwise return DATA
    unchanged (validation, not normalization — the caller reads fields off the same
    dict it validated)."""
    if not isinstance(data, dict):
        raise MarketplaceError("conductor-plugin.json must be a JSON object")
    name = data.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise MarketplaceError(
            f"invalid plugin name {name!r} — must match {NAME_RE.pattern}"
        )
    version = data.get("version")
    if not isinstance(version, str) or not (version == "dev" or VERSION_RE.match(version)):
        raise MarketplaceError(f"invalid version {version!r} — expected vX.Y.Z")
    for field in ("description", "author"):
        v = data.get(field)
        if not isinstance(v, str) or not v.strip():
            raise MarketplaceError(f"manifest missing required field {field!r}")
    backend, frontend = data.get("backend"), data.get("frontend")
    if not isinstance(backend, bool) or not isinstance(frontend, bool):
        raise MarketplaceError("manifest 'backend' and 'frontend' must be true/false")
    if not backend and not frontend:
        raise MarketplaceError("manifest must declare at least one of backend/frontend true")
    min_conductor = data.get("min_conductor")
    if min_conductor is not None and not isinstance(min_conductor, str):
        raise MarketplaceError("manifest 'min_conductor' must be a string if present")
    return data


def _read_manifest(root: Path) -> dict:
    f = root / "conductor-plugin.json"
    if not f.is_file():
        raise MarketplaceError("repo carries no conductor-plugin.json at its root")
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError) as exc:
        raise MarketplaceError(f"conductor-plugin.json is not valid JSON: {exc}")


# --- repo identity --------------------------------------------------------------


_OWNER_NAME_RE = re.compile(r"^[\w.-]+/[\w.-]+$")


def repo_url(repo: str) -> str:
    """A ``git clone``-able URL for REPO — ``owner/name`` shorthand expands to GitHub;
    anything else (a full URL, an ssh remote, or a local path — the shape a test's
    throwaway repo fixture uses) passes through untouched.

    Rejects a value starting with ``-``: every ``git_ops`` call also passes ``--``
    before this string now (belt), but REPO reaches here from index.json entries and
    custom-repo pastes — content this process doesn't author — so refusing the shape
    outright at the one funnel every caller goes through (suspenders) means a future
    ``git_ops`` call that forgets the ``--`` still can't be tricked into treating a
    repo string as a git OPTION instead of the positional repository argument."""
    repo = repo.strip()
    if not repo:
        raise MarketplaceError("repo is required")
    if repo.startswith("-"):
        raise MarketplaceError(f"repo {repo!r} may not start with '-'")
    if _OWNER_NAME_RE.match(repo):
        return f"https://github.com/{repo}.git"
    return repo


def _norm_repo(repo: str) -> str:
    r = repo.strip().rstrip("/")
    if r.endswith(".git"):
        r = r[: -len(".git")]
    return r.lower()


def _safe_norm_repo(repo: str) -> str | None:
    """``_norm_repo(repo_url(repo))``, or None if REPO doesn't even have a valid
    shape (``repo_url`` raises on a leading ``-``). Defense in depth alongside the
    ingestion-time reject in ``_parse_index_payload``: every listing-wide comparison
    below (dedupe, install-status matching) must survive one malformed entry rather
    than 500 the whole read — None never equals a real normalized repo string, so a
    broken entry simply matches nothing instead of raising."""
    try:
        return _norm_repo(repo_url(repo))
    except MarketplaceError:
        return None


def is_http_index_url(source: str) -> bool:
    """The classification rule for one ``marketplace_index_urls`` entry: literally
    "does it start with http:// or https://". Anything else — ``owner/name``
    shorthand, an ssh remote, a bare host path — is a git index source. Deliberately
    NOT "does it look like a git URL": an https:// git remote (e.g. a GitHub clone
    URL) is intentionally still fetched as raw JSON, matching today's behavior for
    every entry already in the wild."""
    return source.strip().lower().startswith(("http://", "https://"))


def index_slug(source: str) -> str:
    """A filesystem-safe, collision-resistant directory name for a git index SOURCE,
    used under ``~/.conductor/marketplace/index/<slug>/``. Built the same way plugin
    names are guarded against path traversal — but a source string isn't NAME_RE-
    shaped (it's a whole URL), so instead of validating it we destroy every character
    that isn't `[a-z0-9-]` outright (a crafted ``../../etc`` collapses to `etc`, never
    escapes) and append a content hash so two sources that collapse to the same
    readable prefix (or an empty one) still land in different directories."""
    base = re.sub(r"[^a-z0-9]+", "-", source.strip().lower()).strip("-")[:40] or "src"
    digest = hashlib.sha256(source.strip().encode()).hexdigest()[:10]
    return f"{base}-{digest}"


# --- state (~/.conductor/marketplace.json) ------------------------------------------


def _read_state() -> dict:
    try:
        raw = json.loads(_STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_state(patch: dict) -> None:
    state = {**_read_state(), **patch}
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def installed_state() -> dict[str, dict]:
    raw = _read_state().get("installed")
    return raw if isinstance(raw, dict) else {}


def pending_apply() -> list[dict]:
    """Every install/update/remove since the last successful ``apply()`` — what the
    UI's persistent banner lists. One row per plugin name (a plugin touched twice
    before an apply keeps only its latest action)."""
    raw = _read_state().get("pending")
    return raw if isinstance(raw, list) else []


def pending_names() -> set[str]:
    """Names with an unapplied install/update/remove staged. A name in this set has a
    STAGED (on-disk) version that may differ from what's actually RUNNING in this
    process — the source-of-truth split the Installed view renders around: while
    pending, "update available" is meaningless noise (the actionable next step is
    Apply, not another Update) so callers suppress that badge for these names."""
    return {p["name"] for p in pending_apply() if p.get("name")}


def _mark_pending(name: str, action: str, *, rebuild: bool) -> None:
    pending = [p for p in pending_apply() if p.get("name") != name]
    pending.append({"name": name, "action": action, "at": utcnow().isoformat(), "rebuild": rebuild})
    _write_state({"pending": pending})


def custom_repo_list() -> list[dict]:
    raw = _read_state().get("custom_repos")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, dict) and c.get("repo")]


async def add_custom_repo(url: str) -> dict:
    """Track URL alongside index entries, resolving its conductor-plugin.json right
    away (best effort — see :func:`resolve_custom_repo_manifest`) so the very next
    ``/index`` read already has a name/description/tags to show instead of a bare
    row. This doubles as the "loading state" for a freshly-added repo: the Add
    button's own busy spinner covers the clone, so the Browse list never needs a
    second, per-row loading affordance."""
    resolved = repo_url(url)  # validates shape; raises MarketplaceError on empty
    norm = _norm_repo(resolved)
    customs = custom_repo_list()
    if any(_norm_repo(repo_url(c["repo"])) == norm for c in customs):
        return {"ok": True, "repo": url, "already_tracked": True}
    manifest = await resolve_custom_repo_manifest(url)
    customs.append({
        "repo": url.strip(),
        "added_at": utcnow().isoformat(),
        "resolved": manifest,
        "resolved_at": utcnow().isoformat() if manifest else None,
    })
    _write_state({"custom_repos": customs})
    return {"ok": True, "repo": url.strip(), "already_tracked": False}


# --- guards shared by install/update -------------------------------------------------


def _shipped_module_names() -> set[str]:
    from .. import MODULES  # deferred: avoids a circular import at package init

    return {r["module"] for r in MODULES if r["source"] == "shipped"}


def _refuse_unmanaged_overwrite(name: str, repo: str) -> None:
    """Refuse to let an install/update clobber something it doesn't own: a shipped
    module's name outright, a local dir the marketplace never created, OR — the
    managed-mismatch case — NAME already installed from a DIFFERENT repo. Two
    different repos can both declare the same plugin `name` in their manifest (an
    honest fork/mirror, or a name collision); without the repo check here, installing
    the second one would silently overwrite the first one's code under its nose. Same
    repo (a real reinstall/update) is the one case this waves through."""
    if name in _shipped_module_names():
        raise MarketplaceError(f"{name!r} is a shipped plugin — a marketplace copy would shadow it")
    installed = installed_state()
    if name in installed:
        installed_repo = installed[name].get("repo", "")
        if _norm_repo(repo_url(installed_repo)) != _norm_repo(repo_url(repo)):
            raise MarketplaceError(
                f"{name!r} is already installed from {installed_repo!r} — refusing to overwrite with {repo!r}",
                status_code=409,
            )
        return  # same repo — ours to replace
    if (_BE_LOCAL / name).is_dir() or (_FE_LOCAL / name).is_dir():
        raise MarketplaceError(
            f"a local plugin directory named {name!r} already exists and isn't marketplace-managed",
            status_code=409,
        )


def _copy_plugin_dirs(src_root: Path, name: str, manifest: dict) -> tuple[bool, bool]:
    """Copy SRC_ROOT/backend → local backend dir and SRC_ROOT/frontend → local frontend
    dir, per the manifest's declared sides — replacing whatever this plugin previously
    had there. A side the manifest DROPPED across an update is removed locally too, so
    a plugin that goes backend-only stops carrying a stale frontend/ copy. Returns
    (has_backend, has_frontend) — what actually landed on disk.

    Validates EVERY declared side exists before copying ANY of them. Copying eagerly
    (backend first, then discovering frontend is missing) would leave a half-installed
    plugin behind with no state entry recorded (``_record_installed`` only runs after
    this returns) — a retry then finds the orphaned backend dir and
    ``_refuse_unmanaged_overwrite`` 409s it forever as "not marketplace-managed",
    since nothing marked it as ours. Validating first makes a bad manifest a no-op."""
    sides = (
        (manifest.get("backend"), "backend", _BE_LOCAL),
        (manifest.get("frontend"), "frontend", _FE_LOCAL),
    )
    for declared, sub, _dest_root in sides:
        if declared and not (src_root / sub).is_dir():
            raise MarketplaceError(f"manifest declares {sub}=true but the repo has no {sub}/ dir")

    has_backend = has_frontend = False
    for declared, sub, dest_root in sides:
        dest = dest_root / name
        src = src_root / sub
        if declared:
            dest_root.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
            if sub == "backend":
                has_backend = True
            else:
                has_frontend = True
        elif dest.is_dir():
            shutil.rmtree(dest)  # side dropped by the author — stop shipping the old copy
    return has_backend, has_frontend


def _record_installed(
    name: str, *, repo: str, ref: str, version: str, has_backend: bool, has_frontend: bool
) -> None:
    installed = installed_state()
    installed[name] = {
        "repo": repo,
        "ref": ref,
        "version": version,
        "installed_at": utcnow().isoformat(),
        "has_backend": has_backend,
        "has_frontend": has_frontend,
    }
    _write_state({"installed": installed})


# --- install / update / remove -------------------------------------------------------


async def install(*, repo: str) -> dict:
    """Clone REPO at its latest semver tag (fallback: default branch HEAD, version
    "dev"), validate its manifest, copy its declared sides into the local plugin dirs,
    and record it as marketplace-managed. Idempotent for a genuine reinstall (same
    name, SAME repo) — an upsert. A name already installed from a DIFFERENT repo is a
    409 (managed-mismatch), not a silent overwrite; see _refuse_unmanaged_overwrite."""
    url = repo_url(repo)
    try:
        tags = await git_ops.remote_tags(url)
    except git_ops.GitError as exc:
        raise MarketplaceError(f"couldn't reach {repo}: {exc}", status_code=502)
    tag = latest_semver_tag(tags)

    tmp = _TMP_DIR / f"install-{uuid.uuid4().hex}"
    try:
        try:
            await git_ops.clone(url, tmp, ref=tag)
        except git_ops.GitError as exc:
            raise MarketplaceError(f"clone failed: {exc}", status_code=502)
        version = tag or "dev"

        manifest = validate_manifest(_read_manifest(tmp))
        name = manifest["name"]
        _refuse_unmanaged_overwrite(name, repo)

        has_backend, has_frontend = _copy_plugin_dirs(tmp, name, manifest)
        # the clone becomes the persistent cache AFTER a successful copy — a failed
        # copy (bad manifest, guard refusal) must not leave an orphan repo cache
        cache_dir = _REPOS_DIR / name
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        _REPOS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp), str(cache_dir))

        _record_installed(
            name, repo=repo, ref=tag or "HEAD", version=version,
            has_backend=has_backend, has_frontend=has_frontend,
        )
        _mark_pending(name, "install", rebuild=has_frontend)
        invalidate_update_cache()
        return {
            "ok": True, "name": name, "version": version,
            "has_backend": has_backend, "has_frontend": has_frontend,
            "apply_needed": True,
        }
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


async def update(name: str) -> dict:
    """Re-check NAME's repo for a newer semver tag and, if found (or if it has never
    carried a tag — track default-branch HEAD instead), re-checkout + re-copy in
    place. Reuses the cached clone via fetch rather than re-cloning; a missing cache
    (deleted by hand, or never created) falls back to a full :func:`install`."""
    info = installed_state().get(name)
    if info is None:
        raise MarketplaceError(f"{name!r} is not installed via the marketplace", status_code=404)
    repo = info["repo"]
    repo_dir = _REPOS_DIR / name
    if not repo_dir.is_dir():
        return await install(repo=repo)

    try:
        await git_ops.fetch_tags(repo_dir)
        tags = await git_ops.local_tags(repo_dir)
    except git_ops.GitError as exc:
        raise MarketplaceError(f"git fetch failed: {exc}", status_code=502)

    tag = latest_semver_tag(tags)
    if tag:
        target_ref, version = tag, tag
    else:
        try:
            await git_ops.fetch_default_head(repo_dir)
        except git_ops.GitError as exc:
            raise MarketplaceError(f"git fetch failed: {exc}", status_code=502)
        target_ref, version = "FETCH_HEAD", "dev"

    try:
        await git_ops.checkout(repo_dir, target_ref)
    except git_ops.GitError as exc:
        raise MarketplaceError(f"git checkout failed: {exc}", status_code=502)

    manifest = validate_manifest(_read_manifest(repo_dir))
    if manifest["name"] != name:
        raise MarketplaceError(
            f"repo's manifest name changed from {name!r} to {manifest['name']!r} — remove and reinstall",
        )
    had_frontend = bool(info.get("has_frontend"))
    has_backend, has_frontend = _copy_plugin_dirs(repo_dir, name, manifest)
    _record_installed(
        name, repo=repo, ref=target_ref, version=version,
        has_backend=has_backend, has_frontend=has_frontend,
    )
    # rebuild is needed if EITHER side of the update touches the frontend bundle — not
    # just the new state: an update that DROPS the frontend (has_frontend False) still
    # deletes files the current dist/ bundle was built from, and conductorctl's own
    # staleness check (`find … -newer dist/index.html`) can only see newer files, never
    # a deletion, so skipping rebuild here would ship a stale bundle with dead code in it.
    _mark_pending(name, "update", rebuild=had_frontend or has_frontend)
    invalidate_update_cache()
    return {
        "ok": True, "name": name, "version": version,
        "has_backend": has_backend, "has_frontend": has_frontend,
        "apply_needed": True,
    }


def remove(name: str) -> dict:
    """Delete the copied local dirs + the state entry — NOT the repo cache (kept so a
    later re-install/update is a fetch, not a fresh clone)."""
    installed = installed_state()
    if name not in installed:
        raise MarketplaceError(f"{name!r} is not installed via the marketplace", status_code=404)
    had_frontend = bool(installed[name].get("has_frontend"))
    removed = []
    for root in (_BE_LOCAL, _FE_LOCAL):
        d = root / name
        if d.is_dir():
            shutil.rmtree(d)
            removed.append(str(d))
    installed.pop(name)
    _write_state({"installed": installed})
    _mark_pending(name, "remove", rebuild=had_frontend)
    invalidate_update_cache()
    return {"ok": True, "name": name, "removed": removed, "apply_needed": True}


# --- readme (pre-install peek) -------------------------------------------------------

_README_CAP = 200_000
_README_CANDIDATES = ("README.md", "README.MD", "Readme.md", "readme.md", "README", "README.rst", "README.txt")


def _readme_cache_key(repo: str, name: str | None) -> str:
    """Which directory under the SAME clone cache install()/update() use holds this
    repo's checkout. A caller-supplied NAME that's already NAME_RE-shaped reuses the
    real install slot ONLY when that name is actually installed FROM THIS SAME repo
    (so an already-installed plugin's README is read straight off its existing
    checkout, no second clone) — never on name alone. Browse can show two different
    entries that happen to declare the same ``name`` from two different repos (a
    fork/mirror, or an honest naming clash — see ``merge_with_custom``'s
    ``name_conflict`` handling); trusting NAME here without checking its repo would
    let a browsed-but-not-installed entry silently display the ALREADY-INSTALLED
    same-named repo's README instead of its own. Anything else (name not installed,
    or installed from a different repo, or no name at all) gets a hashed slot that
    can never collide with a real plugin name (NAME_RE requires a lowercase-letter
    first character; this key always starts with ``_``)."""
    if name and NAME_RE.match(name):
        installed = installed_state().get(name)
        if installed:
            installed_norm = _safe_norm_repo(installed["repo"])
            if installed_norm is not None and installed_norm == _safe_norm_repo(repo):
                return name
    return f"_readme-{index_slug(repo)}"


async def readme_markdown(repo: str, name: str | None = None) -> str | None:
    """REPO's README text (capped at ~200KB), or None if it has none. Reuses an
    existing checkout — installed, or fetched here before — under
    ``~/.conductor/marketplace/repos/``; otherwise shallow-clones into that same
    cache location. This is a PRE-INSTALL peek: nothing is copied into
    ``plugins/local/`` and no state entry is written, so calling it never marks
    anything pending or installed. Raises :class:`MarketplaceError` (502) on a clone
    failure — a private repo the caller's git auth can't reach, an unreachable host,
    etc — so the router can turn it into a structured error instead of a 500."""
    key = _readme_cache_key(repo, name)
    repo_dir = _REPOS_DIR / key
    if not repo_dir.is_dir():
        url = repo_url(repo)
        try:
            await git_ops.clone(url, repo_dir)
        except git_ops.GitError as exc:
            raise MarketplaceError(f"couldn't reach {repo}: {exc}", status_code=502)
    for candidate in _README_CANDIDATES:
        f = repo_dir / candidate
        if f.is_file():
            return f.read_text(errors="replace")[:_README_CAP]
    return None


# --- index (browse) -----------------------------------------------------------------

_INDEX_REFRESH_TTL_S = 600
# ttl_key string -> last refresh attempt (monotonic seconds), successful or not — a
# broken remote gets re-tried at most once per TTL, not on every /index call. Shared
# shape for both git-sourced indexes and custom-repo manifest resolution (below);
# each caller passes its own dict so the two domains can never collide.
_INDEX_PULL_ATTEMPTED: dict[str, float] = {}


async def _cached_checkout(
    cache_dir: Path, url: str, ttl_cache: dict[str, float], ttl_key: str, *, ttl_seconds: float = _INDEX_REFRESH_TTL_S,
) -> Path | None:
    """The local mirror of URL at CACHE_DIR: cloned on first sight, fast-forward-
    pulled at most once per TTL_SECONDS thereafter. A clone/pull failure — including
    a private repo the host's git auth can't reach — degrades to "serve whatever is
    already on disk" (a failed pull leaves the prior working tree untouched) and,
    with nothing on disk yet, to None (caller skips the source). A repeatedly-failing
    clone is ALSO backed off by TTL_KEY — without this, a permanently unreachable
    source would retry on every single call instead of at most once per window."""
    now = time.monotonic()
    last = ttl_cache.get(ttl_key, 0.0)
    if not cache_dir.is_dir():
        if now - last < ttl_seconds:
            return None  # a recent clone attempt already failed — don't hammer it
        ttl_cache[ttl_key] = now
        try:
            await git_ops.clone(url, cache_dir)
        except git_ops.GitError as exc:
            log.warning("[marketplace] clone failed for %s: %s", url, exc)
            return None
        return cache_dir

    if now - last >= ttl_seconds:
        ttl_cache[ttl_key] = now
        try:
            await git_ops.pull_ff_only(cache_dir)
        except git_ops.GitError as exc:
            log.warning(
                "[marketplace] refresh failed for %s — serving the last cached copy: %s", url, exc,
            )
    return cache_dir


async def _git_index_repo_dir(source: str) -> Path | None:
    """The local mirror of a git-sourced index — see :func:`_cached_checkout`."""
    return await _cached_checkout(_INDEX_DIR / index_slug(source), repo_url(source), _INDEX_PULL_ATTEMPTED, source)


def _parse_index_payload(data: object, entries: list[dict], seen: set[str]) -> None:
    """Append every well-formed ``{name, repo, description?, tags?}`` row in DATA's
    ``"plugins"`` list to ENTRIES, deduped against SEEN (first source wins) — the one
    parsing routine shared by the http and git index paths so they can never drift.

    Rejects a ``repo`` starting with ``-`` here too, at the point of ingestion — the
    SAME shape ``repo_url()`` refuses (git argument injection). Index content is
    third-party and this endpoint's whole contract is "one bad source is logged and
    skipped, never fatal" — without this check here, a single malformed row reaches
    ``repo_url()`` unguarded later (in ``merge_with_custom`` / ``with_install_status``)
    and raises, which would take down the ENTIRE ``/index`` read instead of just that
    one row."""
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, list):
        return
    for p in plugins:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "").strip()
        repo = str(p.get("repo") or "").strip()
        if not name or not repo or repo.startswith("-") or name in seen:
            continue
        seen.add(name)
        tags = p.get("tags")
        entries.append({
            "name": name,
            "repo": repo,
            "description": str(p.get("description") or ""),
            "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
            "source": "index",
        })


async def fetch_index(urls: list[str], *, timeout: float = 10) -> list[dict]:
    """Every plugin entry from every reachable index source, deduped by name (first
    source wins). Each entry in URLS is classified by :func:`is_http_index_url`: an
    http(s) URL is GET-fetched as raw JSON (today's behavior, unchanged); anything
    else is a git repo, cloned/pulled into a local mirror (so a PRIVATE index works
    through the host's own git auth, same as a plugin install) and read from disk. A
    single bad/unreachable source is logged and skipped — never blocks the whole
    listing, since the index is third-party content the user doesn't control."""
    entries: list[dict] = []
    seen: set[str] = set()

    git_sources = [u for u in urls if not is_http_index_url(u)]
    http_sources = [u for u in urls if is_http_index_url(u)]

    for source in git_sources:
        repo_dir = await _git_index_repo_dir(source)
        if repo_dir is None:
            continue
        f = repo_dir / "index.json"
        if not f.is_file():
            log.warning("[marketplace] index source %s carries no index.json at its root", source)
            continue
        try:
            data = json.loads(f.read_text())
        except (OSError, ValueError) as exc:
            log.warning("[marketplace] index.json in %s is not valid JSON: %s", source, exc)
            continue
        _parse_index_payload(data, entries, seen)

    if http_sources:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for url in http_sources:
                try:
                    r = await client.get(url)
                    r.raise_for_status()
                    data = r.json()
                except Exception as exc:  # noqa: BLE001 — a bad index entry must not 500 /index
                    log.warning("[marketplace] index fetch failed for %s: %s", url, exc)
                    continue
                _parse_index_payload(data, entries, seen)

    return entries


_CUSTOM_RESOLVE_ATTEMPTED: dict[str, float] = {}


async def resolve_custom_repo_manifest(repo: str) -> dict | None:
    """Best-effort ``{name, description, version, tags}`` for a custom-added REPO,
    read from its ``conductor-plugin.json`` via the SAME clone-cache machinery the
    readme endpoint uses (``_readme_cache_key`` with no name yet known → the hashed
    ``_readme-<slug>`` slot) — so browsing a custom repo and opening its install
    dialog's README share one clone instead of two. None on ANY failure (unreachable
    repo, no manifest, invalid JSON, or a manifest missing the two fields a display
    row actually needs) — the caller keeps the row with just the raw repo string and
    a "manifest unavailable" flag; this is a read-only peek, same as readme_markdown,
    so a broken manifest here is never install-blocking."""
    checkout = await _cached_checkout(
        _REPOS_DIR / _readme_cache_key(repo, None), repo_url(repo), _CUSTOM_RESOLVE_ATTEMPTED, repo,
    )
    if checkout is None:
        return None
    f = checkout / "conductor-plugin.json"
    if not f.is_file():
        return None
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    description = data.get("description")
    if not isinstance(name, str) or not NAME_RE.match(name) or not isinstance(description, str) or not description.strip():
        return None  # the two fields a display row needs — anything less isn't a usable manifest
    tags = data.get("tags")
    version = data.get("version")
    return {
        "name": name,
        "description": description,
        "version": version if isinstance(version, str) else None,
        "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
    }


async def merge_with_custom(index_entries: list[dict]) -> list[dict]:
    """INDEX_ENTRIES plus every tracked custom repo — what the Browse tab renders.
    Each custom repo's manifest is (re-)resolved here, subject to the same 10-min TTL
    as a git index source (so a page load never re-clones on every read); any newly-
    resolved metadata is persisted back to ``custom_repos`` in ONE write, so the NEXT
    load renders instantly with no network/git call at all.

    Exactly ONE dedupe rule, and it is about the REPO, never the name: a custom entry
    is hidden only when its repo normalizes to the same value as an index entry's (or
    an earlier custom entry's) — a genuine duplicate of something already shown. A
    custom repo whose RESOLVED name merely COLLIDES with another entry's name, while
    being a different repo (a fork/mirror, or an honest naming clash), is NOT hidden:
    dropping it would make it unreachable in the UI — no Install, and worse, no
    Remove, since a hidden row still sits in ``custom_repos`` state as a ghost the
    user can't act on. Instead it renders with ``name_conflict: true`` so the FE can
    flag it, while keeping its own Install/Remove actions intact. The first entry to
    claim a name (index entries first, then customs in list order) is the one every
    later same-named-different-repo entry is compared against."""
    out = list(index_entries)
    known_repos = {r for e in out if (r := _safe_norm_repo(e["repo"])) is not None}
    known_names = {e["name"] for e in out if e.get("name")}

    customs = custom_repo_list()
    updated: list[dict] = []
    dirty = False
    for c in customs:
        now = time.monotonic()
        stale = now - _CUSTOM_RESOLVE_ATTEMPTED.get(c["repo"], 0.0) >= _INDEX_REFRESH_TTL_S
        if c.get("resolved") is None or stale:
            manifest = await resolve_custom_repo_manifest(c["repo"])
            if manifest != c.get("resolved"):
                c = {**c, "resolved": manifest, "resolved_at": utcnow().isoformat() if manifest else c.get("resolved_at")}
                dirty = True
        updated.append(c)

        norm_repo = _safe_norm_repo(c["repo"])
        if norm_repo is not None and norm_repo in known_repos:
            continue  # a true duplicate of an already-shown repo — the only case that hides
        if norm_repo is not None:
            known_repos.add(norm_repo)

        manifest = c.get("resolved")
        name_conflict = bool(manifest and manifest.get("name") in known_names)
        if manifest and not name_conflict:
            known_names.add(manifest["name"])
        out.append({
            "name": manifest["name"] if manifest else None,
            "repo": c["repo"],
            "description": manifest["description"] if manifest else "",
            "tags": manifest["tags"] if manifest else [],
            "version": manifest.get("version") if manifest else None,
            "source": "custom",
            "manifest_unavailable": manifest is None,
            "name_conflict": name_conflict,
        })

    if dirty:
        _write_state({"custom_repos": updated})
    return out


def with_install_status(entries: list[dict]) -> list[dict]:
    """ENTRIES annotated with whether (and as what installed name/version) each is
    already installed — matched by repo, not name, since a custom entry has no name
    until it's actually installed."""
    installed = installed_state()
    # installed repos are always already-validated (repo_url succeeded at install
    # time) — only the per-ENTRY lookup below touches untrusted third-party content.
    by_repo = {_norm_repo(repo_url(info["repo"])): (n, info) for n, info in installed.items()}
    out = []
    for e in entries:
        match = by_repo.get(_safe_norm_repo(e["repo"]))
        row = dict(e)
        if match:
            iname, info = match
            row.update(installed=True, installed_name=iname, installed_version=info.get("version"))
        else:
            row.update(installed=False, installed_name=None, installed_version=None)
        out.append(row)
    return out


_UPDATE_CACHE: dict = {"ts": 0.0, "data": None}
_UPDATE_CACHE_TTL_S = 600


def invalidate_update_cache() -> None:
    """Drop the cached ``check_updates()`` result — called by install/update/remove so
    a mutation is reflected on the very next ``/installed`` read instead of showing
    stale current/latest versions (or a missing row for a plugin just installed) for
    up to the full 10-minute TTL."""
    _UPDATE_CACHE["ts"], _UPDATE_CACHE["data"] = 0.0, None


async def _tag_comparison(info: dict) -> dict:
    """{current_version, latest_version, update_available, error} for one installed
    plugin's INFO record — the one tag-comparison routine shared by the list-wide
    (cached) :func:`check_updates` and the per-repo (always-fresh) :func:`check_update_for`,
    so the two can never disagree about what "update available" means."""
    try:
        tags = await git_ops.remote_tags(repo_url(info["repo"]))
        latest = latest_semver_tag(tags)
    except git_ops.GitError:
        return {"current_version": info.get("version"), "latest_version": None, "update_available": False, "error": True}
    current = info.get("version")
    return {
        "current_version": current, "latest_version": latest,
        "update_available": bool(latest) and latest != current, "error": False,
    }


async def check_updates(*, force: bool = False) -> dict:
    """Per installed (github-sourced) plugin: is there a newer semver tag upstream?
    Cached like the plugin manager's ``/check-updates`` — this hits the network per
    installed repo, and the badge shouldn't cost a round trip on every panel render."""
    now = time.monotonic()
    if not force and _UPDATE_CACHE["data"] is not None and now - _UPDATE_CACHE["ts"] < _UPDATE_CACHE_TTL_S:
        return _UPDATE_CACHE["data"]

    out: dict[str, dict] = {}

    async def one(name: str, info: dict) -> None:
        out[name] = await _tag_comparison(info)

    await asyncio.gather(*(one(n, i) for n, i in installed_state().items()))
    result = {"updates": out, "checked_at": utcnow().isoformat()}
    _UPDATE_CACHE["ts"], _UPDATE_CACHE["data"] = now, result
    return result


async def check_update_for(name: str) -> dict:
    """A FRESH (never cached, never written into ``_UPDATE_CACHE``) tag comparison for
    ONE installed plugin — what the update dialog calls the moment it opens, so a tag
    pushed moments ago is reflected immediately instead of waiting out the list-wide
    10-minute TTL. Deliberately bypasses the cache entirely rather than reading OR
    populating it: a single-repo check must not silently skew list-wide data with a
    partial update, and the list view already gets its own eventual refresh."""
    info = installed_state().get(name)
    if info is None:
        raise MarketplaceError(f"{name!r} is not installed via the marketplace", status_code=404)
    return {"name": name, **await _tag_comparison(info)}


# --- apply (build + restart) ---------------------------------------------------------


def _clear_pending_shell_snippet() -> str:
    """A single shell command that clears ``pending`` in the on-disk state file,
    preserving every other key — meant to run INSIDE the detached apply script,
    AFTER any build step and right before the final restart exec. This runs in the
    script, not synchronously in :func:`apply`, on purpose: clearing it the instant
    the script is merely LAUNCHED would report "nothing pending" while a slow
    frontend build (often much longer than a few seconds) is still in flight — the
    exact bug a reviewer caught (a reload during that window shows the banner gone
    with the change not actually live yet). Uses the stdlib only (no venv activation
    needed) via a plain ``python3`` on PATH, matching every other host tool this
    script already assumes (``git``, ``npm``, ``bash`` itself)."""
    return (
        'python3 -c "'
        "import json,pathlib; "
        f"p=pathlib.Path(r'{_STATE_FILE}'); "
        "d=json.loads(p.read_text()) if p.exists() else {}; "
        "d['pending']=[]; "
        'p.write_text(json.dumps(d, indent=2, ensure_ascii=False)+chr(10))'
        '"\n'
    )


async def apply() -> dict:
    """Rebuild the frontend (only if something pending touches it) and restart via the
    SAME mechanism ``bin/conductorctl restart`` uses — detached in its own session (the
    updater / plugin-manager pattern) so the restart it triggers cannot kill it
    mid-script. Never signals the running process directly (no pkill, no kill -9):
    ``conductorctl restart`` is the one blessed path, ``launchctl kickstart`` under it.

    Does NOT clear pending state itself — see :func:`_clear_pending_shell_snippet`."""
    pending = pending_apply()
    rebuild = any(p.get("rebuild") for p in pending)
    build = ""
    if rebuild:
        build = (
            'command -v npm >/dev/null 2>&1 || { for d in "$HOME"/.nvm/versions/node/*/bin; do PATH="$d:$PATH"; done; }\n'
            "npm --prefix frontend run build || echo '!! frontend build failed — restarting on the old bundle'\n"
        )
    script = (
        f'exec >> "{_APPLY_LOG}" 2>&1\n'
        f'echo "== marketplace apply $(date) rebuild={rebuild}"\n'
        f'cd "{_ROOT}"\n'
        f"{build}"
        f"{_clear_pending_shell_snippet()}"
        f'exec "{_ROOT}/bin/conductorctl" restart\n'
    )
    _APPLY_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", script,
            start_new_session=True,  # survives the backend it is about to restart
        )
    except OSError as exc:
        raise MarketplaceError(f"couldn't launch apply: {exc}", status_code=500)
    return {"ok": True, "restarting": True, "rebuild": rebuild}
