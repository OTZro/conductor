"""Marketplace plugin: manifest validation, name sanitization, semver tag selection,
and the install/update/remove state machine.

Git operations run against REAL throwaway repos built in ``tmp_path`` (``git init`` +
commit + tag), cloned by local filesystem path — no mocked subprocess, no network.
Every marketplace path (local plugin dirs, repo cache, state file) is monkeypatched to
``tmp_path`` so a run never touches this checkout or the real ``~/.conductor``."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from conductor.config import settings
from conductor.main import app
from conductor.plugins.marketplace import git_ops, service
from conductor.plugins.marketplace.service import MarketplaceError

# asyncio_mode = "auto" (backend/pyproject.toml) — `async def test_...` needs no
# @pytest.mark.asyncio decorator here.


# --- repo fixture helpers -------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_repo(
    tmp_path: Path, name: str = "widget", *, version: str = "v1.0.0",
    backend: bool = True, frontend: bool = True, tag: bool = True,
) -> Path:
    """A fresh git repo (one commit) at TMP_PATH/src-<name>, with a conductor-plugin.json
    manifest and whichever of backend/ frontend/ are requested. Tagged VERSION unless
    ``tag=False`` (the no-tags / "dev" install case)."""
    root = tmp_path / f"src-{name}-{version}"
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    manifest = {
        "name": name, "version": version, "description": "A widget plugin",
        "author": "tester", "backend": backend, "frontend": frontend,
    }
    (root / "conductor-plugin.json").write_text(json.dumps(manifest))
    if backend:
        bdir = root / "backend"
        bdir.mkdir()
        (bdir / "__init__.py").write_text("MARK = 1\n")
    if frontend:
        fdir = root / "frontend"
        fdir.mkdir()
        (fdir / "index.tsx").write_text("export const TAB_LAYOUTS = {};\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", f"release {version}", cwd=root)
    if tag:
        _git("tag", version, cwd=root)
    return root


def bump(root: Path, version: str, *, marker: str = "bump") -> None:
    """A second commit + tag on an existing repo — the update path's "newer version
    landed upstream" scenario. MARKER.txt at the repo ROOT is for tests that only
    care "did we fetch past this commit" (it's never copied — _copy_plugin_dirs only
    touches backend/frontend); when frontend/ exists, its index.tsx is ALSO rewritten
    with MARKER so a test can verify actual copied CONTENT changed, not just that
    version metadata was bumped."""
    manifest = json.loads((root / "conductor-plugin.json").read_text())
    manifest["version"] = version
    (root / "conductor-plugin.json").write_text(json.dumps(manifest))
    (root / "MARKER.txt").write_text(marker)
    fe = root / "frontend" / "index.tsx"
    if fe.is_file():
        fe.write_text(f"export const TAB_LAYOUTS = {{}}; // {marker}\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", f"release {version}", cwd=root)
    _git("tag", version, cwd=root)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Every marketplace path lives under tmp_path for the duration of the test — no
    real ~/.conductor or checkout dir is ever touched."""
    monkeypatch.setattr(service, "_BE_LOCAL", tmp_path / "be_local")
    monkeypatch.setattr(service, "_FE_LOCAL", tmp_path / "fe_local")
    monkeypatch.setattr(service, "_REPOS_DIR", tmp_path / "mkt_repos")
    monkeypatch.setattr(service, "_TMP_DIR", tmp_path / "mkt_tmp")
    monkeypatch.setattr(service, "_INDEX_DIR", tmp_path / "mkt_index")
    monkeypatch.setattr(service, "_STATE_FILE", tmp_path / "marketplace.json")
    monkeypatch.setattr(service, "_APPLY_LOG", tmp_path / "logs" / "marketplace-apply.log")
    service._UPDATE_CACHE["ts"] = 0.0
    service._UPDATE_CACHE["data"] = None
    service._INDEX_PULL_ATTEMPTED.clear()
    service._CUSTOM_RESOLVE_ATTEMPTED.clear()


# --- name sanitization -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["widget", "widget-2", "widget_two", "ab"])
def test_name_regex_accepts_valid_names(name):
    assert service.NAME_RE.match(name)


@pytest.mark.parametrize(
    "name", ["Widget", "2widget", "widget!", "wid get", "../evil", "", "a", "w" * 42]
)
def test_name_regex_rejects_invalid_names(name):
    assert not service.NAME_RE.match(name)


# --- manifest validation ----------------------------------------------------------------


def _valid_manifest(**overrides) -> dict:
    base = {
        "name": "widget", "version": "v1.0.0", "description": "d",
        "author": "a", "backend": True, "frontend": False,
    }
    base.update(overrides)
    return base


def test_validate_manifest_accepts_a_well_formed_manifest():
    assert service.validate_manifest(_valid_manifest())["name"] == "widget"


def test_validate_manifest_accepts_dev_version():
    assert service.validate_manifest(_valid_manifest(version="dev"))


@pytest.mark.parametrize("bad_name", ["Widget", "1widget", "../evil", ""])
def test_validate_manifest_rejects_bad_name(bad_name):
    with pytest.raises(MarketplaceError, match="invalid plugin name"):
        service.validate_manifest(_valid_manifest(name=bad_name))


@pytest.mark.parametrize("bad_version", ["1.0", "v1.0", "latest", "", "v1.0.0-rc1"])
def test_validate_manifest_rejects_bad_version(bad_version):
    with pytest.raises(MarketplaceError, match="invalid version"):
        service.validate_manifest(_valid_manifest(version=bad_version))


@pytest.mark.parametrize("field", ["description", "author"])
def test_validate_manifest_rejects_missing_required_field(field):
    with pytest.raises(MarketplaceError, match="missing required field"):
        service.validate_manifest(_valid_manifest(**{field: ""}))


def test_validate_manifest_rejects_neither_backend_nor_frontend():
    with pytest.raises(MarketplaceError, match="at least one"):
        service.validate_manifest(_valid_manifest(backend=False, frontend=False))


def test_validate_manifest_rejects_non_bool_backend():
    with pytest.raises(MarketplaceError, match="true/false"):
        service.validate_manifest(_valid_manifest(backend="yes"))


def test_validate_manifest_rejects_non_dict():
    with pytest.raises(MarketplaceError, match="JSON object"):
        service.validate_manifest(["not", "a", "dict"])


def test_validate_manifest_rejects_non_string_min_conductor():
    with pytest.raises(MarketplaceError, match="min_conductor"):
        service.validate_manifest(_valid_manifest(min_conductor=1))


# --- semver tag selection ---------------------------------------------------------------


def test_latest_semver_tag_picks_the_highest():
    assert service.latest_semver_tag(["v1.0.0", "v1.2.0", "v1.1.9"]) == "v1.2.0"


def test_latest_semver_tag_ignores_non_semver_tags():
    assert service.latest_semver_tag(["release", "v1.0.0", "not-a-tag"]) == "v1.0.0"


def test_latest_semver_tag_none_when_no_tags_parse():
    assert service.latest_semver_tag(["latest", "unstable"]) is None


def test_latest_semver_tag_none_on_empty_list():
    assert service.latest_semver_tag([]) is None


def test_latest_semver_tag_handles_double_digit_components():
    assert service.latest_semver_tag(["v1.9.0", "v1.10.0"]) == "v1.10.0"


# --- git_ops against a real local repo ---------------------------------------------------


async def test_remote_tags_lists_tags_from_a_real_repo(tmp_path):
    repo = make_repo(tmp_path, version="v1.0.0")
    assert await git_ops.remote_tags(str(repo)) == ["v1.0.0"]


async def test_remote_tags_empty_for_untagged_repo(tmp_path):
    repo = make_repo(tmp_path, version="v1.0.0", tag=False)
    assert await git_ops.remote_tags(str(repo)) == []


async def test_clone_at_ref_checks_out_that_tag(tmp_path):
    repo = make_repo(tmp_path, version="v1.0.0")
    bump(repo, "v1.1.0")
    dest = tmp_path / "cloned"
    await git_ops.clone(str(repo), dest, ref="v1.0.0")
    manifest = json.loads((dest / "conductor-plugin.json").read_text())
    assert manifest["version"] == "v1.0.0"
    assert not (dest / "MARKER.txt").exists()  # that file only exists from v1.1.0 onward


# --- install ------------------------------------------------------------------------------


async def test_install_with_tags_installs_the_latest_tag(tmp_path):
    repo = make_repo(tmp_path, name="widget", version="v1.0.0")
    bump(repo, "v1.1.0")
    result = await service.install(repo=str(repo))
    assert result == {
        "ok": True, "name": "widget", "version": "v1.1.0",
        "has_backend": True, "has_frontend": True, "apply_needed": True,
    }
    assert (service._BE_LOCAL / "widget" / "__init__.py").is_file()
    assert (service._FE_LOCAL / "widget" / "index.tsx").is_file()
    installed = service.installed_state()["widget"]
    assert installed["version"] == "v1.1.0" and installed["repo"] == str(repo)


async def test_install_without_tags_installs_as_dev(tmp_path):
    repo = make_repo(tmp_path, name="widget", version="v1.0.0", tag=False)
    result = await service.install(repo=str(repo))
    assert result["version"] == "dev"
    assert service.installed_state()["widget"]["version"] == "dev"


async def test_install_backend_only_manifest_copies_only_backend(tmp_path):
    repo = make_repo(tmp_path, name="beonly", frontend=False)
    result = await service.install(repo=str(repo))
    assert result["has_backend"] is True and result["has_frontend"] is False
    assert (service._BE_LOCAL / "beonly").is_dir()
    assert not (service._FE_LOCAL / "beonly").exists()


async def test_install_records_pending_apply_with_rebuild_flag(tmp_path):
    repo = make_repo(tmp_path, name="widget")  # has frontend
    await service.install(repo=str(repo))
    pending = service.pending_apply()
    assert len(pending) == 1
    assert pending[0]["name"] == "widget" and pending[0]["action"] == "install"
    assert pending[0]["rebuild"] is True


async def test_install_refuses_a_bad_manifest_name(tmp_path):
    repo = make_repo(tmp_path, name="widget")
    manifest = json.loads((repo / "conductor-plugin.json").read_text())
    manifest["name"] = "Bad Name"
    (repo / "conductor-plugin.json").write_text(json.dumps(manifest))
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "bad name", cwd=repo)
    _git("tag", "v1.0.1", cwd=repo)
    with pytest.raises(MarketplaceError, match="invalid plugin name"):
        await service.install(repo=str(repo))


async def test_install_refuses_shipped_module_name_collision(tmp_path):
    """`manager` is a real shipped module in this checkout — a marketplace copy under
    that name would shadow it, which the shipped-collision guard exists to prevent."""
    repo = make_repo(tmp_path, name="manager")
    with pytest.raises(MarketplaceError, match="shipped plugin"):
        await service.install(repo=str(repo))


async def test_install_refuses_overwriting_an_unmanaged_local_dir(tmp_path):
    (service._BE_LOCAL / "widget").mkdir(parents=True)
    (service._BE_LOCAL / "widget" / "__init__.py").write_text("# hand-written\n")
    repo = make_repo(tmp_path, name="widget")
    with pytest.raises(MarketplaceError, match="isn't marketplace-managed") as ei:
        await service.install(repo=str(repo))
    assert ei.value.status_code == 409
    # untouched — the guard fires before anything is copied
    assert (service._BE_LOCAL / "widget" / "__init__.py").read_text() == "# hand-written\n"


async def test_install_is_idempotent_for_a_marketplace_managed_name(tmp_path):
    repo = make_repo(tmp_path, name="widget", version="v1.0.0")
    await service.install(repo=str(repo))
    result = await service.install(repo=str(repo))  # reinstall — must not 409 on itself
    assert result["ok"] is True and result["name"] == "widget"


async def test_install_unreachable_repo_raises_502(tmp_path):
    with pytest.raises(MarketplaceError) as ei:
        await service.install(repo=str(tmp_path / "does-not-exist"))
    assert ei.value.status_code == 502


async def test_install_validates_every_declared_side_before_copying_any(tmp_path):
    """A manifest declaring BOTH sides, with only backend/ actually present, must fail
    WITHOUT leaving a half-installed backend dir behind — copying backend first and
    THEN discovering frontend/ is missing would orphan an unmanaged, un-recorded local
    dir that 409s every retry as "not marketplace-managed" (the bug two reviewers
    independently caught: Codex + CodeRabbit)."""
    repo = make_repo(tmp_path, name="widget")  # both sides declared + present
    shutil.rmtree(repo / "frontend")  # ...now only backend/ actually exists
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "drop frontend without updating the manifest", cwd=repo)
    _git("tag", "v1.0.1", cwd=repo)

    with pytest.raises(MarketplaceError, match="frontend/ dir"):
        await service.install(repo=str(repo))

    assert not (service._BE_LOCAL / "widget").exists()  # nothing copied — not even backend
    assert "widget" not in service.installed_state()
    assert service.pending_apply() == []

    # the repo is fixable (declare backend-only) and a subsequent install must not
    # 409 as "unmanaged" — proof no orphan dir was left behind by the failed attempt
    manifest = json.loads((repo / "conductor-plugin.json").read_text())
    manifest["frontend"] = False
    (repo / "conductor-plugin.json").write_text(json.dumps(manifest))
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "fix manifest", cwd=repo)
    _git("tag", "v1.0.2", cwd=repo)
    result = await service.install(repo=str(repo))
    assert result["ok"] is True and result["has_backend"] is True


# --- update -------------------------------------------------------------------------------


async def test_update_picks_up_a_newer_tag(tmp_path):
    repo = make_repo(tmp_path, name="widget", version="v1.0.0")
    await service.install(repo=str(repo))
    bump(repo, "v1.1.0", marker="v1.1.0-content")
    result = await service.update("widget")
    assert result["version"] == "v1.1.0"
    # actual COPIED content changed (bump() rewrites frontend/index.tsx), not just
    # version metadata — MARKER.txt itself lives at the repo root and is never
    # copied, so asserting on it (as an earlier version of this test did) would pass
    # even if _copy_plugin_dirs re-copied nothing at all.
    assert "v1.1.0-content" in (service._FE_LOCAL / "widget" / "index.tsx").read_text()
    assert service.installed_state()["widget"]["version"] == "v1.1.0"
    assert service.installed_state()["widget"]["ref"] == "v1.1.0"


async def test_update_that_drops_frontend_still_requests_a_rebuild(tmp_path):
    """An update that goes from frontend-enabled to backend-only still deletes files
    the CURRENT dist/ bundle was built from — conductorctl's staleness check only
    sees newer files, never a deletion, so skipping rebuild here would ship a stale
    bundle carrying the now-uninstalled plugin's dead frontend code."""
    repo = make_repo(tmp_path, name="widget")  # frontend=True
    await service.install(repo=str(repo))
    manifest = json.loads((repo / "conductor-plugin.json").read_text())
    manifest["frontend"] = False
    manifest["version"] = "v1.1.0"
    (repo / "conductor-plugin.json").write_text(json.dumps(manifest))
    shutil.rmtree(repo / "frontend")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "drop frontend", cwd=repo)
    _git("tag", "v1.1.0", cwd=repo)

    result = await service.update("widget")
    assert result["has_frontend"] is False
    pending = {p["name"]: p for p in service.pending_apply()}
    assert pending["widget"]["rebuild"] is True  # not False, despite has_frontend now False


async def test_update_with_no_new_tag_is_a_noop_reinstall(tmp_path):
    repo = make_repo(tmp_path, name="widget", version="v1.0.0")
    await service.install(repo=str(repo))
    result = await service.update("widget")
    assert result["version"] == "v1.0.0"


async def test_update_tracks_head_when_repo_never_tagged(tmp_path):
    repo = make_repo(tmp_path, name="widget", tag=False)
    await service.install(repo=str(repo))
    bump(repo, "dev", marker="second commit")  # no tag — untagged repos stay on branch HEAD
    result = await service.update("widget")
    assert result["version"] == "dev"


async def test_update_unknown_name_raises_404(tmp_path):
    with pytest.raises(MarketplaceError) as ei:
        await service.update("nope")
    assert ei.value.status_code == 404


async def test_update_falls_back_to_reinstall_when_cache_missing(tmp_path):
    repo = make_repo(tmp_path, name="widget", version="v1.0.0")
    await service.install(repo=str(repo))
    shutil.rmtree(service._REPOS_DIR / "widget")  # simulate a hand-deleted cache
    bump(repo, "v1.1.0")
    result = await service.update("widget")
    assert result["version"] == "v1.1.0"


# --- remove -------------------------------------------------------------------------------


async def test_remove_deletes_local_dirs_but_keeps_repo_cache(tmp_path):
    repo = make_repo(tmp_path, name="widget")
    await service.install(repo=str(repo))
    assert (service._REPOS_DIR / "widget").is_dir()
    result = service.remove("widget")
    assert result["ok"] is True
    assert not (service._BE_LOCAL / "widget").exists()
    assert not (service._FE_LOCAL / "widget").exists()
    assert "widget" not in service.installed_state()
    assert (service._REPOS_DIR / "widget").is_dir()  # cache left behind, per the brief


async def test_remove_unknown_name_raises_404():
    with pytest.raises(MarketplaceError) as ei:
        service.remove("nope")
    assert ei.value.status_code == 404


async def test_remove_then_reinstall_is_clean(tmp_path):
    repo = make_repo(tmp_path, name="widget")
    await service.install(repo=str(repo))
    service.remove("widget")
    result = await service.install(repo=str(repo))
    assert result["ok"] is True
    assert (service._BE_LOCAL / "widget").is_dir()


# --- pending-apply state model ------------------------------------------------------------
# A name with an unapplied install/update is STAGED (on disk) but not yet RUNNING (loaded
# in this process) — pending_names()/the /installed row's pending_apply flag is that split,
# and it must suppress "update available" so the UI never offers an update on top of one
# that hasn't taken effect yet.


async def test_pending_names_reflects_the_pending_list(tmp_path):
    assert service.pending_names() == set()
    repo = make_repo(tmp_path, name="widget")
    await service.install(repo=str(repo))
    assert service.pending_names() == {"widget"}


async def test_check_update_for_is_never_cached(tmp_path):
    repo = make_repo(tmp_path, name="widget", version="v1.0.0")
    await service.install(repo=str(repo))
    service._UPDATE_CACHE["ts"] = time.monotonic()
    service._UPDATE_CACHE["data"] = {"updates": {}, "checked_at": "stale"}
    bump(repo, "v1.1.0")
    result = await service.check_update_for("widget")
    assert result == {
        "name": "widget", "current_version": "v1.0.0", "latest_version": "v1.1.0",
        "update_available": True, "error": False,
    }
    # a single-repo check must not have populated the list-wide cache either
    assert service._UPDATE_CACHE["data"] == {"updates": {}, "checked_at": "stale"}


async def test_check_update_for_equal_versions_reports_no_update(tmp_path):
    repo = make_repo(tmp_path, name="widget", version="v1.0.0")
    await service.install(repo=str(repo))
    result = await service.check_update_for("widget")
    assert result["current_version"] == result["latest_version"] == "v1.0.0"
    assert result["update_available"] is False


async def test_check_update_for_unknown_name_raises_404():
    with pytest.raises(MarketplaceError) as ei:
        await service.check_update_for("nope")
    assert ei.value.status_code == 404


# --- update-cache invalidation -----------------------------------------------------------
# A cached check_updates() result must not survive a mutation — otherwise /installed can
# omit a just-installed plugin's status, or keep showing an updated plugin's OLD version
# and a stale "update available" badge, for up to the full 10-minute TTL.


async def test_install_invalidates_the_update_cache(tmp_path):
    service._UPDATE_CACHE["ts"] = time.monotonic()
    service._UPDATE_CACHE["data"] = {"updates": {}, "checked_at": "stale"}
    repo = make_repo(tmp_path, name="widget")
    await service.install(repo=str(repo))
    assert service._UPDATE_CACHE["data"] is None


async def test_update_invalidates_the_update_cache(tmp_path):
    repo = make_repo(tmp_path, name="widget", version="v1.0.0")
    await service.install(repo=str(repo))
    bump(repo, "v1.1.0")
    service._UPDATE_CACHE["ts"] = time.monotonic()
    service._UPDATE_CACHE["data"] = {"updates": {"widget": {"current_version": "v1.0.0"}}, "checked_at": "stale"}
    await service.update("widget")
    assert service._UPDATE_CACHE["data"] is None


async def test_remove_invalidates_the_update_cache(tmp_path):
    repo = make_repo(tmp_path, name="widget")
    await service.install(repo=str(repo))
    service._UPDATE_CACHE["ts"] = time.monotonic()
    service._UPDATE_CACHE["data"] = {"updates": {"widget": {}}, "checked_at": "stale"}
    service.remove("widget")
    assert service._UPDATE_CACHE["data"] is None


async def test_installed_reflects_a_fresh_install_immediately_despite_a_recent_cache(tmp_path):
    """The end-to-end shape of the bug: a cached (empty) check_updates() result from
    BEFORE an install must not make the freshly-installed plugin's update row vanish
    on the very next /installed-equivalent read."""
    service._UPDATE_CACHE["ts"] = time.monotonic()
    service._UPDATE_CACHE["data"] = {"updates": {}, "checked_at": "stale"}
    repo = make_repo(tmp_path, name="widget")
    await service.install(repo=str(repo))
    checks = await service.check_updates()
    assert "widget" in checks["updates"]


# --- repo argument-injection guard --------------------------------------------------------
# repo strings arrive from third-party content (index.json entries, custom-repo pastes) —
# a value starting with "-" could otherwise be parsed by git as an OPTION instead of the
# positional repository, a well-known class of bug for any CLI wrapper shelling out to git.


@pytest.mark.parametrize("evil", ["-oProxyCommand=x", "--upload-pack=x", "-x"])
def test_repo_url_rejects_a_leading_dash(evil):
    with pytest.raises(MarketplaceError, match="may not start with"):
        service.repo_url(evil)


async def test_install_rejects_a_leading_dash_repo(tmp_path):
    with pytest.raises(MarketplaceError, match="may not start with"):
        await service.install(repo="--upload-pack=touch /tmp/pwned")


async def test_git_ops_clone_treats_a_dash_prefixed_url_as_a_path_not_an_option(tmp_path):
    """Defense in depth below repo_url()'s own reject: even if some future caller
    handed git_ops a dash-prefixed value directly, the ``--`` separator must make git
    treat it as a (nonexistent) path/repo, never as an unrecognized OPTION — proven by
    the failure being "repository not found"-shaped, not an "unknown option" error.
    Destination lives under tmp_path — clone() rmtree's an existing destination
    before running, so a shared fixed path could race/clobber something real."""
    with pytest.raises(git_ops.GitError) as ei:
        await git_ops.clone("-x", tmp_path / "should-not-be-created-by-this-test")
    message = str(ei.value).lower()
    assert "unrecognized option" not in message and "unknown option" not in message


async def test_git_ops_remote_tags_treats_a_dash_prefixed_url_as_a_path_not_an_option():
    with pytest.raises(git_ops.GitError) as ei:
        await git_ops.remote_tags("-x")
    message = str(ei.value).lower()
    assert "unrecognized option" not in message and "unknown option" not in message


# --- custom repos + browse merge -----------------------------------------------------------


async def _no_manifest(_repo: str) -> None:
    """Stub for resolve_custom_repo_manifest in tests that aren't about resolution
    itself — avoids a real network clone against a fake github.com/someorg/somerepo."""
    return None


async def test_add_custom_repo_tracks_it(monkeypatch):
    monkeypatch.setattr(service, "resolve_custom_repo_manifest", _no_manifest)
    result = await service.add_custom_repo("someorg/somerepo")
    assert result["ok"] is True and result["already_tracked"] is False
    assert any(c["repo"] == "someorg/somerepo" for c in service.custom_repo_list())


async def test_add_custom_repo_dedups_by_normalized_url(monkeypatch):
    monkeypatch.setattr(service, "resolve_custom_repo_manifest", _no_manifest)
    await service.add_custom_repo("someorg/somerepo")
    result = await service.add_custom_repo("https://github.com/someorg/somerepo.git")
    assert result["already_tracked"] is True
    assert len(service.custom_repo_list()) == 1


async def test_merge_with_custom_skips_repos_already_in_the_index(monkeypatch):
    monkeypatch.setattr(service, "resolve_custom_repo_manifest", _no_manifest)
    await service.add_custom_repo("someorg/somerepo")
    index_entries = [{"name": "x", "repo": "someorg/somerepo", "description": "", "tags": [], "source": "index"}]
    merged = await service.merge_with_custom(index_entries)
    assert len(merged) == 1  # not duplicated


# --- custom repo manifest resolution (round 5) ----------------------------------------------


def make_manifest_only_repo(tmp_path: Path, dirname: str, manifest: dict | str) -> Path:
    """A real throwaway git repo carrying conductor-plugin.json at its root — for
    resolve_custom_repo_manifest tests. MANIFEST as a str writes it verbatim (the
    broken-JSON case); as a dict it's json.dumped."""
    root = tmp_path / dirname
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    text = manifest if isinstance(manifest, str) else json.dumps(manifest)
    (root / "conductor-plugin.json").write_text(text)
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "manifest", cwd=root)
    return root


async def test_resolve_custom_repo_manifest_reads_name_description_version_tags(tmp_path):
    repo = make_manifest_only_repo(tmp_path, "resolvable", {
        "name": "widget", "version": "v2.0.0", "description": "A widget", "author": "a",
        "backend": True, "frontend": False, "tags": ["fun", "small"],
    })
    manifest = await service.resolve_custom_repo_manifest(str(repo))
    assert manifest == {"name": "widget", "description": "A widget", "version": "v2.0.0", "tags": ["fun", "small"]}


async def test_resolve_custom_repo_manifest_none_when_manifest_missing(tmp_path):
    root = tmp_path / "no-manifest"
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "README.md").write_text("nothing here")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "no manifest", cwd=root)
    assert await service.resolve_custom_repo_manifest(str(root)) is None


async def test_resolve_custom_repo_manifest_none_on_broken_json(tmp_path):
    repo = make_manifest_only_repo(tmp_path, "broken-json", "{not valid json")
    assert await service.resolve_custom_repo_manifest(str(repo)) is None


async def test_resolve_custom_repo_manifest_none_when_name_invalid(tmp_path):
    repo = make_manifest_only_repo(tmp_path, "bad-name", {"name": "Bad Name", "description": "d"})
    assert await service.resolve_custom_repo_manifest(str(repo)) is None


async def test_resolve_custom_repo_manifest_none_when_unreachable(tmp_path):
    assert await service.resolve_custom_repo_manifest(str(tmp_path / "does-not-exist")) is None


async def test_add_custom_repo_persists_resolved_manifest(tmp_path):
    repo = make_manifest_only_repo(tmp_path, "resolvable", {
        "name": "widget", "version": "v1.0.0", "description": "A widget", "author": "a",
        "backend": True, "frontend": False,
    })
    await service.add_custom_repo(str(repo))
    [entry] = service.custom_repo_list()
    assert entry["resolved"]["name"] == "widget"
    assert entry["resolved_at"] is not None


async def test_merge_with_custom_shows_resolved_metadata(tmp_path):
    repo = make_manifest_only_repo(tmp_path, "resolvable", {
        "name": "widget", "version": "v1.0.0", "description": "A widget", "author": "a",
        "backend": True, "frontend": False, "tags": ["x"],
    })
    await service.add_custom_repo(str(repo))
    [row] = await service.merge_with_custom([])
    assert row["name"] == "widget" and row["description"] == "A widget"
    assert row["version"] == "v1.0.0" and row["tags"] == ["x"]
    assert row["manifest_unavailable"] is False


async def test_merge_with_custom_flags_manifest_unavailable(tmp_path):
    await service.add_custom_repo(str(tmp_path / "does-not-exist"))
    [row] = await service.merge_with_custom([])
    assert row["name"] is None and row["manifest_unavailable"] is True


async def test_merge_with_custom_reuses_persisted_metadata_without_reresolving(tmp_path, monkeypatch):
    """Within the TTL window, a second /index-equivalent read must not re-clone/pull —
    the persisted `resolved` field from the first read is what renders instantly."""
    repo = make_manifest_only_repo(tmp_path, "resolvable", {
        "name": "widget", "version": "v1.0.0", "description": "A widget", "author": "a",
        "backend": True, "frontend": False,
    })
    await service.add_custom_repo(str(repo))

    calls = {"n": 0}

    async def counting_resolve(_repo: str):
        calls["n"] += 1
        return None

    monkeypatch.setattr(service, "resolve_custom_repo_manifest", counting_resolve)
    await service.merge_with_custom([])
    await service.merge_with_custom([])
    assert calls["n"] == 0  # persisted from add_custom_repo(), still fresh — no re-resolve


async def test_merge_with_custom_same_repo_still_dedupes_against_index(tmp_path):
    """The pre-existing rule, unchanged: a custom repo whose URL normalizes to the
    SAME value as an index entry's is a true duplicate — hidden."""
    index_entries = [{"name": "widget", "repo": "the-real-owner/widget", "description": "canonical", "tags": [], "source": "index"}]
    await service.add_custom_repo("https://github.com/the-real-owner/widget.git")
    merged = await service.merge_with_custom(index_entries)
    assert len(merged) == 1
    assert merged[0]["repo"] == "the-real-owner/widget"


async def test_merge_with_custom_different_repo_name_collision_renders_both_rows(tmp_path):
    """A custom repo whose RESOLVED name collides with an index entry, but is a
    DIFFERENT repo (a fork/mirror or an honest naming clash), must NOT disappear —
    dropping it would make it unreachable (no Install, no Remove). Both rows render,
    each with its own repo; the custom one carries name_conflict for the FE to flag."""
    mirror = make_manifest_only_repo(tmp_path, "mirror", {
        "name": "widget", "version": "v1.0.0", "description": "A mirrored widget", "author": "a",
        "backend": True, "frontend": False,
    })
    await service.add_custom_repo(str(mirror))
    index_entries = [{"name": "widget", "repo": "the-real-owner/widget", "description": "canonical", "tags": [], "source": "index"}]
    merged = await service.merge_with_custom(index_entries)
    assert len(merged) == 2
    by_repo = {e["repo"]: e for e in merged}
    assert not by_repo["the-real-owner/widget"].get("name_conflict")  # index entries never carry the flag
    assert by_repo[str(mirror)]["name_conflict"] is True
    assert by_repo[str(mirror)]["name"] == "widget"  # metadata still shown, not suppressed


async def test_merge_with_custom_name_conflict_row_is_still_removable(tmp_path):
    """A name-conflicting custom row is a real (non-ghost) entry: it can still be
    removed via the ordinary custom-repo/remove path — nothing about the conflict
    flag blocks that, since it's a display annotation, not a state change."""
    mirror = make_manifest_only_repo(tmp_path, "mirror", {
        "name": "widget", "version": "v1.0.0", "description": "A mirrored widget", "author": "a",
        "backend": True, "frontend": False,
    })
    await service.add_custom_repo(str(mirror))
    index_entries = [{"name": "widget", "repo": "the-real-owner/widget", "description": "canonical", "tags": [], "source": "index"}]
    merged = await service.merge_with_custom(index_entries)
    assert any(e["repo"] == str(mirror) and e["name_conflict"] for e in merged)
    # ghost-removability: the row is reachable and its repo can be dropped from state
    customs = service.custom_repo_list()
    assert any(c["repo"] == str(mirror) for c in customs)
    remaining = [c for c in customs if c["repo"] != str(mirror)]
    service._write_state({"custom_repos": remaining})
    assert not any(c["repo"] == str(mirror) for c in service.custom_repo_list())
    merged_after = await service.merge_with_custom(index_entries)
    assert not any(e["repo"] == str(mirror) for e in merged_after)


async def test_install_refuses_a_different_repo_under_an_already_installed_name(tmp_path):
    """The managed-mismatch guard: NAME already installed from repo A must 409 —
    never silently overwrite — when a DIFFERENT repo B declares the same name. The
    error names both repos."""
    repo_a = make_repo(tmp_path, name="widget")
    await service.install(repo=str(repo_a))
    repo_b = make_manifest_only_repo(tmp_path, "widget-b", {
        "name": "widget", "version": "v1.0.0", "description": "a different widget", "author": "a",
        "backend": True, "frontend": False,
    })
    with pytest.raises(MarketplaceError) as ei:
        await service.install(repo=str(repo_b))
    assert ei.value.status_code == 409
    assert str(repo_a) in ei.value.message and str(repo_b) in ei.value.message
    # the original install is untouched
    assert service.installed_state()["widget"]["repo"] == str(repo_a)


async def test_install_same_repo_under_an_already_installed_name_still_upserts(tmp_path):
    """The SAME repo re-installing under the name it already owns is the ordinary
    reinstall/update path, not a managed-mismatch — must not 409."""
    repo = make_repo(tmp_path, name="widget", version="v1.0.0")
    await service.install(repo=str(repo))
    result = await service.install(repo=str(repo))
    assert result["ok"] is True


async def test_merge_with_custom_still_installable_when_manifest_unavailable(tmp_path):
    """An unresolvable custom repo stays in the list (installable/removable) — it's
    just missing display metadata, never dropped outright."""
    repo_dir = tmp_path / "does-not-exist"
    await service.add_custom_repo(str(repo_dir))
    merged = await service.merge_with_custom([])
    assert len(merged) == 1 and merged[0]["repo"] == str(repo_dir)


async def test_with_install_status_matches_installed_by_repo(tmp_path):
    repo = make_repo(tmp_path, name="widget")
    await service.install(repo=str(repo))
    entries = [{"name": "widget", "repo": str(repo), "description": "", "tags": [], "source": "index"}]
    [row] = service.with_install_status(entries)
    assert row["installed"] is True
    assert row["installed_name"] == "widget"
    assert row["installed_version"] == "v1.0.0"


def test_with_install_status_reports_not_installed_for_unknown_repo():
    entries = [{"name": "x", "repo": "someorg/somerepo", "description": "", "tags": [], "source": "index"}]
    [row] = service.with_install_status(entries)
    assert row["installed"] is False


# --- git-sourced index (private index repos) ------------------------------------------------
# A marketplace_index_urls entry that isn't http(s):// is a git repo, mirrored locally so a
# private index works through the host's own git auth — same trust model as a plugin install.


def make_index_repo(tmp_path: Path, plugins: list[dict], *, dirname: str = "idx") -> Path:
    """A real throwaway git repo carrying an index.json at its root — no tag needed,
    the index path only ever reads whatever is currently checked out."""
    root = tmp_path / dirname
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "index.json").write_text(json.dumps({"plugins": plugins}))
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "index", cwd=root)
    return root


def test_is_http_index_url_classifies_by_scheme_only():
    assert service.is_http_index_url("https://example.com/index.json") is True
    assert service.is_http_index_url("http://example.com/index.json") is True
    assert service.is_http_index_url("owner/name") is False
    assert service.is_http_index_url("git@github.com:owner/name.git") is False
    assert service.is_http_index_url("https://github.com/owner/name.git") is True  # scheme wins, not shape


def test_index_slug_is_deterministic_and_collision_resistant():
    assert service.index_slug("owner/name") == service.index_slug("owner/name")
    assert service.index_slug("owner/name") != service.index_slug("owner/other")


@pytest.mark.parametrize("evil", ["../../../etc/passwd", "..\\..\\windows", "/etc/passwd", "a/../../b"])
def test_index_slug_never_escapes_its_directory(evil, tmp_path):
    slug = service.index_slug(evil)
    resolved = (tmp_path / slug).resolve()
    assert resolved.parent == tmp_path.resolve()  # stays a direct child — no traversal
    assert ".." not in slug and "/" not in slug and "\\" not in slug


async def test_fetch_index_merges_a_git_source_with_an_http_source(tmp_path, monkeypatch):
    git_repo = make_index_repo(tmp_path, [{"name": "gitty", "repo": "org/gitty", "description": "d", "tags": []}])

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"plugins": [{"name": "webby", "repo": "org/webby", "description": "d", "tags": []}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            return FakeResp()

    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kw: FakeClient())
    entries = await service.fetch_index([str(git_repo), "https://example.com/index.json"])
    names = {e["name"] for e in entries}
    assert names == {"gitty", "webby"}


async def test_fetch_index_clones_a_git_source_on_first_read(tmp_path):
    git_repo = make_index_repo(tmp_path, [{"name": "gitty", "repo": "org/gitty", "description": "", "tags": []}])
    entries = await service.fetch_index([str(git_repo)])
    assert [e["name"] for e in entries] == ["gitty"]
    assert (service._INDEX_DIR / service.index_slug(str(git_repo)) / "index.json").is_file()


async def test_fetch_index_unreachable_git_source_is_skipped_not_fatal(tmp_path):
    entries = await service.fetch_index([str(tmp_path / "does-not-exist")])
    assert entries == []


async def test_fetch_index_pull_failure_serves_the_last_cached_copy(tmp_path, monkeypatch):
    """The private-repo-goes-unreachable scenario: the mirror was cloned successfully
    earlier, the remote is now unreachable (revoked auth, network blip, deleted repo),
    and the index endpoint must still answer with whatever was last pulled — not 500,
    not an empty list."""
    git_repo = make_index_repo(tmp_path, [{"name": "gitty", "repo": "org/gitty", "description": "", "tags": []}])
    first = await service.fetch_index([str(git_repo)])
    assert [e["name"] for e in first] == ["gitty"]

    # force a refresh attempt (bypass the TTL) against a now-broken remote
    service._INDEX_PULL_ATTEMPTED.clear()

    async def broken_pull(repo_dir):
        raise git_ops.GitError("could not read from remote repository")

    monkeypatch.setattr(git_ops, "pull_ff_only", broken_pull)
    second = await service.fetch_index([str(git_repo)])
    assert [e["name"] for e in second] == ["gitty"]  # degraded gracefully, served from disk


async def test_fetch_index_respects_the_ttl_and_does_not_pull_every_call(tmp_path, monkeypatch):
    git_repo = make_index_repo(tmp_path, [{"name": "gitty", "repo": "org/gitty", "description": "", "tags": []}])
    await service.fetch_index([str(git_repo)])  # first call: clone, no pull

    calls = {"n": 0}

    async def counting_pull(repo_dir):
        calls["n"] += 1

    monkeypatch.setattr(git_ops, "pull_ff_only", counting_pull)
    await service.fetch_index([str(git_repo)])
    await service.fetch_index([str(git_repo)])
    assert calls["n"] == 0  # within the TTL — no pull attempted on either follow-up call


async def test_fetch_index_pulls_again_once_the_ttl_expires(tmp_path, monkeypatch):
    git_repo = make_index_repo(tmp_path, [{"name": "gitty", "repo": "org/gitty", "description": "", "tags": []}])
    await service.fetch_index([str(git_repo)])

    calls = {"n": 0}

    async def counting_pull(repo_dir):
        calls["n"] += 1

    monkeypatch.setattr(git_ops, "pull_ff_only", counting_pull)
    service._INDEX_PULL_ATTEMPTED[str(git_repo)] = time.monotonic() - service._INDEX_REFRESH_TTL_S - 1
    await service.fetch_index([str(git_repo)])
    assert calls["n"] == 1


async def test_fetch_index_git_source_with_no_index_json_is_skipped(tmp_path):
    root = tmp_path / "empty-idx"
    root.mkdir()
    _git("init", "-q", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "README.md").write_text("nothing here")
    _git("add", "-A", cwd=root)
    _git("commit", "-q", "-m", "no index", cwd=root)
    entries = await service.fetch_index([str(root)])
    assert entries == []


async def test_merge_with_custom_still_works_alongside_a_git_index_source(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "resolve_custom_repo_manifest", _no_manifest)
    git_repo = make_index_repo(tmp_path, [{"name": "gitty", "repo": "org/gitty", "description": "", "tags": []}])
    entries = await service.fetch_index([str(git_repo)])
    await service.add_custom_repo("someorg/somerepo")
    merged = await service.merge_with_custom(entries)
    assert {e["repo"] for e in merged} == {"org/gitty", "someorg/somerepo"}


# --- regression: one malformed index entry must not take down the whole /index read ------
# CodeRabbit caught this as a real regression from the leading-dash repo_url() guard: an
# index.json is third-party content this server does not control, and the contract (already
# tested above for a bad URL / missing index.json / unreachable source) is "log + skip that
# ONE source" — it must extend to "skip that one ROW", not crash every entry in the payload.


def test_parse_index_payload_skips_a_dash_prefixed_repo_but_keeps_good_rows():
    entries: list[dict] = []
    seen: set[str] = set()
    payload = {"plugins": [
        {"name": "good", "repo": "owner/good", "description": "d", "tags": []},
        {"name": "evil", "repo": "-oProxyCommand=x", "description": "d", "tags": []},
        {"name": "also-good", "repo": "owner/also-good", "description": "d", "tags": []},
    ]}
    service._parse_index_payload(payload, entries, seen)  # must not raise
    assert {e["name"] for e in entries} == {"good", "also-good"}


async def test_fetch_index_survives_a_malformed_repo_entry(tmp_path):
    git_repo = make_index_repo(tmp_path, [
        {"name": "good", "repo": "owner/good", "description": "d", "tags": []},
        {"name": "evil", "repo": "-x", "description": "d", "tags": []},
    ])
    entries = await service.fetch_index([str(git_repo)])  # must not raise
    assert [e["name"] for e in entries] == ["good"]


async def test_merge_with_custom_survives_a_malformed_index_entry():
    """The crash this regression test targets happened INSIDE merge_with_custom (the
    known_repos comprehension calls repo_url() on every entry) — reachable even if a
    future ingestion path ever forgot _parse_index_payload's own guard, which is
    exactly why _safe_norm_repo exists as defense in depth, not just the ingestion
    check alone."""
    index_entries = [
        {"name": "good", "repo": "owner/good", "description": "", "tags": [], "source": "index"},
        {"name": "evil", "repo": "-x", "description": "", "tags": [], "source": "index"},
    ]
    merged = await service.merge_with_custom(index_entries)  # must not raise
    assert {e["name"] for e in merged} == {"good", "evil"}  # both pass through unharmed


async def test_index_endpoint_returns_200_with_good_entries_when_one_is_malformed(monkeypatch, tmp_path):
    """End to end: GET /index must still be a 200 carrying the good entries — never a
    500/400 — when one source's index.json contains a hostile/malformed repo row."""
    git_repo = make_index_repo(tmp_path, [
        {"name": "good", "repo": "owner/good", "description": "d", "tags": []},
        {"name": "evil", "repo": "--upload-pack=touch pwned", "description": "d", "tags": []},
    ])
    monkeypatch.setattr(settings, "marketplace_index_urls", str(git_repo))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        r = await c.get("/api/plugins/marketplace/index")
    assert r.status_code == 200
    names = {p["name"] for p in r.json()["plugins"]}
    assert "good" in names and "evil" not in names


# --- readme cache key: must not leak an installed repo's README to a different repo ------


async def test_readme_cache_key_falls_back_when_name_is_installed_from_a_different_repo(tmp_path):
    installed_repo = make_repo(tmp_path, name="widget")
    await service.install(repo=str(installed_repo))
    other_repo = "some/other-repo"
    key = service._readme_cache_key(other_repo, "widget")
    assert key != "widget"
    assert key.startswith("_readme-")


async def test_readme_markdown_does_not_leak_an_installed_repos_readme_to_a_same_named_different_repo(tmp_path):
    installed_repo = make_repo(tmp_path, name="widget")
    (installed_repo / "README.md").write_text("installed widget's real README")
    _git("add", "-A", cwd=installed_repo)
    _git("commit", "-q", "-m", "readme", cwd=installed_repo)
    _git("tag", "v1.0.1", cwd=installed_repo)
    await service.install(repo=str(installed_repo))

    # a DIFFERENT repo that happens to also declare/browse under the name "widget"
    impostor_repo = make_manifest_only_repo(tmp_path, "impostor", {
        "name": "widget", "version": "v1.0.0", "description": "an impostor", "author": "a",
        "backend": True, "frontend": False,
    })
    (impostor_repo / "README.md").write_text("impostor's own README")
    _git("add", "-A", cwd=impostor_repo)
    _git("commit", "-q", "-m", "readme", cwd=impostor_repo)

    text = await service.readme_markdown(str(impostor_repo), "widget")
    assert text == "impostor's own README"  # NOT the installed widget's README


# --- readme (pre-install peek) ---------------------------------------------------------


async def test_readme_markdown_reads_from_a_real_repo(tmp_path):
    repo = make_repo(tmp_path, name="widget")
    (repo / "README.md").write_text("# Widget\n\nDoes widget things.")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "readme", cwd=repo)
    text = await service.readme_markdown(str(repo))
    assert text is not None and "# Widget" in text and "Does widget things." in text


async def test_readme_markdown_none_when_repo_has_no_readme(tmp_path):
    repo = make_repo(tmp_path, name="widget")
    assert await service.readme_markdown(str(repo)) is None


async def test_readme_markdown_reuses_the_install_cache_by_name(tmp_path):
    """A NAME_RE-shaped `name` reuses the very directory install()/update() use — an
    already-installed (or about-to-be, under that name) plugin's README is read
    straight off its existing checkout, no second clone."""
    repo = make_repo(tmp_path, name="widget")
    (repo / "README.md").write_text("v1 readme")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "readme", cwd=repo)
    _git("tag", "v1.0.1", cwd=repo)  # move the tag forward so install() picks up the README
    await service.install(repo=str(repo))
    assert (service._REPOS_DIR / "widget").is_dir()
    text = await service.readme_markdown(str(repo), "widget")
    assert text == "v1 readme"


async def test_readme_markdown_unreachable_repo_raises_structured_error(tmp_path):
    with pytest.raises(MarketplaceError) as ei:
        await service.readme_markdown(str(tmp_path / "does-not-exist"))
    assert ei.value.status_code == 502


async def test_readme_markdown_caps_at_200kb(tmp_path):
    repo = make_repo(tmp_path, name="widget")
    (repo / "README.md").write_text("x" * 300_000)
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "big readme", cwd=repo)
    text = await service.readme_markdown(str(repo))
    assert text is not None and len(text) == service._README_CAP


@pytest.mark.parametrize("evil_name", ["../../etc", "Bad Name", "", "2widget"])
async def test_readme_cache_key_rejects_hostile_name_and_falls_back_to_a_hashed_slot(evil_name):
    """A `name` that isn't NAME_RE-shaped never reaches the filesystem as a path
    component — it's routed to the hashed `_readme-<slug>` slot instead."""
    key = service._readme_cache_key("owner/name", evil_name)
    assert key.startswith("_readme-")
    assert ".." not in key and "/" not in key


# --- pending / apply ------------------------------------------------------------------------


async def test_apply_launches_detached_and_does_not_clear_pending_synchronously(tmp_path, monkeypatch):
    """pending must survive the (mocked, never-actually-run) launch: clearing it the
    instant the script is merely spawned — before any build/restart has actually
    happened — is exactly the premature-clear bug a reviewer caught. The real clear
    now happens INSIDE the script (see test_apply_script_clears_pending_state_*
    below), which this test can't observe since the subprocess is mocked."""
    repo = make_repo(tmp_path, name="widget")
    await service.install(repo=str(repo))
    assert service.pending_apply()

    launched = {}

    async def fake_exec(*args, **kwargs):
        launched["args"] = args
        launched["kwargs"] = kwargs

        class P:
            pass

        return P()

    monkeypatch.setattr(service.asyncio, "create_subprocess_exec", fake_exec)
    result = await service.apply()
    assert result == {"ok": True, "restarting": True, "rebuild": True}
    assert launched["kwargs"]["start_new_session"] is True
    assert service.pending_apply() != []  # still there — the clearing script never ran


async def test_apply_script_clears_pending_state_right_before_restart(tmp_path):
    """The shell snippet embedded in the apply script, executed for REAL (not
    mocked), correctly clears `pending` while leaving every other state key alone."""
    service._write_state({
        "installed": {"widget": {"repo": "x"}},
        "pending": [{"name": "widget", "action": "install", "rebuild": True}],
        "custom_repos": [{"repo": "someorg/somerepo"}],
    })
    snippet = service._clear_pending_shell_snippet()
    proc = await asyncio.create_subprocess_exec("/bin/bash", "-c", snippet)
    await proc.wait()
    assert proc.returncode == 0
    assert service.pending_apply() == []
    assert service.installed_state() == {"widget": {"repo": "x"}}
    assert service.custom_repo_list() == [{"repo": "someorg/somerepo"}]


async def test_apply_script_contains_the_pending_clear_step_before_restart(tmp_path, monkeypatch):
    """The generated script must run the clear step BEFORE the final restart exec —
    order matters (that's the whole point of moving it out of synchronous Python)."""
    repo = make_repo(tmp_path, name="widget")
    await service.install(repo=str(repo))

    captured = {}

    async def fake_exec(*args, **kwargs):
        captured["script"] = args[2]  # ("/bin/bash", "-c", script)

        class P:
            pass

        return P()

    monkeypatch.setattr(service.asyncio, "create_subprocess_exec", fake_exec)
    await service.apply()
    script = captured["script"]
    clear_pos = script.index("d['pending']=[]")
    restart_pos = script.index("conductorctl\" restart")
    assert clear_pos < restart_pos


async def test_apply_skips_rebuild_when_nothing_pending_needs_it(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, name="beonly", frontend=False)
    await service.install(repo=str(repo))

    async def fake_exec(*args, **kwargs):
        class P:
            pass

        return P()

    monkeypatch.setattr(service.asyncio, "create_subprocess_exec", fake_exec)
    result = await service.apply()
    assert result["rebuild"] is False


# --- router / CSRF gate -----------------------------------------------------------------
# Mirrors test_updater.py: install/update/remove/custom_repo/apply are CORS *simple*
# POSTs (no body-shape requirement blocks a cross-site form), so they must all reject a
# missing/foreign Origin the same way the updater plugin's mutating routes do.


@pytest.fixture
async def api(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.mark.parametrize(
    "path", [
        "/api/plugins/marketplace/install", "/api/plugins/marketplace/update",
        "/api/plugins/marketplace/remove", "/api/plugins/marketplace/custom_repo",
        "/api/plugins/marketplace/apply",
    ],
)
async def test_mutating_endpoints_reject_a_foreign_origin(api, path):
    r = await api.post(path, json={}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


@pytest.mark.parametrize(
    "path", [
        "/api/plugins/marketplace/install", "/api/plugins/marketplace/update",
        "/api/plugins/marketplace/remove", "/api/plugins/marketplace/custom_repo",
        "/api/plugins/marketplace/apply",
    ],
)
async def test_mutating_endpoints_reject_a_missing_origin(api, path):
    assert (await api.post(path, json={})).status_code == 403


async def test_index_endpoint_is_a_plain_open_get(api):
    r = await api.get("/api/plugins/marketplace/index")
    assert r.status_code == 200
    assert r.json() == {"plugins": []}  # no index urls configured, no custom repos


async def test_installed_endpoint_is_a_plain_open_get(api):
    r = await api.get("/api/plugins/marketplace/installed")
    assert r.status_code == 200
    assert r.json()["plugins"] == []


async def test_install_endpoint_installs_a_repo_with_same_origin(api, tmp_path):
    repo = make_repo(tmp_path, name="widget")
    origin = f"http://127.0.0.1:{settings.port}"
    r = await api.post(
        "/api/plugins/marketplace/install", json={"repo": str(repo)}, headers={"Origin": origin}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["name"] == "widget"


async def test_install_endpoint_404s_on_an_unknown_index_name(api):
    origin = f"http://127.0.0.1:{settings.port}"
    r = await api.post(
        "/api/plugins/marketplace/install", json={"name": "nope"}, headers={"Origin": origin}
    )
    assert r.status_code == 404


async def test_custom_repo_endpoint_tracks_a_repo(api, monkeypatch):
    monkeypatch.setattr(service, "resolve_custom_repo_manifest", _no_manifest)
    origin = f"http://127.0.0.1:{settings.port}"
    r = await api.post(
        "/api/plugins/marketplace/custom_repo", json={"url": "someorg/somerepo"}, headers={"Origin": origin}
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    listed = await api.get("/api/plugins/marketplace/index")
    assert any(p["repo"] == "someorg/somerepo" for p in listed.json()["plugins"])


async def test_remove_endpoint_404s_for_unknown_name(api):
    origin = f"http://127.0.0.1:{settings.port}"
    r = await api.post(
        "/api/plugins/marketplace/remove", json={"name": "nope"}, headers={"Origin": origin}
    )
    assert r.status_code == 404


async def test_installed_endpoint_marks_a_freshly_installed_plugin_pending_apply(api, tmp_path):
    repo = make_repo(tmp_path, name="widget")
    origin = f"http://127.0.0.1:{settings.port}"
    await api.post("/api/plugins/marketplace/install", json={"repo": str(repo)}, headers={"Origin": origin})
    r = await api.get("/api/plugins/marketplace/installed")
    [row] = r.json()["plugins"]
    assert row["pending_apply"] is True
    assert row["update_available"] is False  # suppressed while pending, regardless of the tag check


async def test_check_update_endpoint_rejects_a_foreign_origin(api):
    r = await api.post(
        "/api/plugins/marketplace/check_update", json={"name": "widget"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


async def test_check_update_endpoint_rejects_a_missing_origin(api):
    r = await api.post("/api/plugins/marketplace/check_update", json={"name": "widget"})
    assert r.status_code == 403


async def test_check_update_endpoint_returns_fresh_comparison(api, tmp_path):
    repo = make_repo(tmp_path, name="widget", version="v1.0.0")
    origin = f"http://127.0.0.1:{settings.port}"
    await api.post("/api/plugins/marketplace/install", json={"repo": str(repo)}, headers={"Origin": origin})
    bump(repo, "v1.1.0")
    r = await api.post(
        "/api/plugins/marketplace/check_update", json={"name": "widget"}, headers={"Origin": origin}
    )
    assert r.status_code == 200
    assert r.json()["latest_version"] == "v1.1.0"


async def test_check_update_endpoint_404s_for_unknown_name(api):
    origin = f"http://127.0.0.1:{settings.port}"
    r = await api.post(
        "/api/plugins/marketplace/check_update", json={"name": "nope"}, headers={"Origin": origin}
    )
    assert r.status_code == 404


# --- readme endpoint ---------------------------------------------------------------------
# POST, not GET: a browser only sends `Origin` on a same-origin GET when the request
# mode forces it, which `fetch()` does not by default — gating a GET route 403s every
# normal same-origin caller (this is exactly the round-3 bug: the origin-gate tests
# above covered explicit-Origin and no-Origin as pass/fail cases, but a real same-
# origin GET's "no Origin" IS the normal case, not the attack case). POST carries
# Origin unconditionally, so gate and route agree — same fix shape as the round-3 CSRF
# gate itself, applied to the one route that got the method wrong.


async def test_readme_endpoint_get_is_gone(api):
    """The old GET route must not silently keep working (and must not 500) — either
    a plain 404 or a 405 (path known, wrong method) is an acceptable "gone", but a 200
    would mean the bug is still there."""
    r = await api.get("/api/plugins/marketplace/readme", params={"repo": "owner/name"})
    assert r.status_code in (404, 405)


async def test_readme_endpoint_rejects_a_foreign_origin(api):
    r = await api.post(
        "/api/plugins/marketplace/readme", json={"repo": "owner/name"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403


async def test_readme_endpoint_rejects_a_missing_origin(api):
    r = await api.post("/api/plugins/marketplace/readme", json={"repo": "owner/name"})
    assert r.status_code == 403


async def test_readme_endpoint_returns_markdown_with_same_origin(api, tmp_path):
    repo = make_repo(tmp_path, name="widget")
    (repo / "README.md").write_text("hello from readme")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "readme", cwd=repo)
    origin = f"http://127.0.0.1:{settings.port}"
    r = await api.post(
        "/api/plugins/marketplace/readme", json={"repo": str(repo)}, headers={"Origin": origin}
    )
    assert r.status_code == 200 and r.json() == {"markdown": "hello from readme"}


async def test_readme_endpoint_returns_null_markdown_when_absent(api, tmp_path):
    repo = make_repo(tmp_path, name="widget")
    origin = f"http://127.0.0.1:{settings.port}"
    r = await api.post(
        "/api/plugins/marketplace/readme", json={"repo": str(repo)}, headers={"Origin": origin}
    )
    assert r.status_code == 200 and r.json() == {"markdown": None}


async def test_readme_endpoint_unreachable_repo_is_a_structured_error_not_a_500(api, tmp_path):
    origin = f"http://127.0.0.1:{settings.port}"
    r = await api.post(
        "/api/plugins/marketplace/readme", json={"repo": str(tmp_path / "nope")},
        headers={"Origin": origin},
    )
    assert r.status_code == 502
    assert "detail" in r.json()  # structured JSON body, not a raw traceback


async def test_readme_endpoint_requires_a_repo_field(api):
    origin = f"http://127.0.0.1:{settings.port}"
    r = await api.post("/api/plugins/marketplace/readme", json={"repo": ""}, headers={"Origin": origin})
    assert r.status_code == 400


async def test_readme_endpoint_accepts_a_name_field(api, tmp_path):
    repo = make_repo(tmp_path, name="widget")
    (repo / "README.md").write_text("v1 readme")
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "readme", cwd=repo)
    _git("tag", "v1.0.1", cwd=repo)
    await service.install(repo=str(repo))
    origin = f"http://127.0.0.1:{settings.port}"
    r = await api.post(
        "/api/plugins/marketplace/readme", json={"repo": str(repo), "name": "widget"},
        headers={"Origin": origin},
    )
    assert r.status_code == 200 and r.json() == {"markdown": "v1 readme"}
