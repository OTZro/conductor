"""Marketplace routes — mounted at ``/api/plugins/marketplace``.

Reads (``/index``, ``/installed``) stay open to a plain GET. Every mutating route
(install/update/remove/custom_repo/apply) is a CORS *simple* POST — no body-shape
requirement stops a foreign page from firing one — so they all go through the same
Origin check the updater plugin uses: installing third-party code is exactly the kind
of side effect a page must not be able to trigger cross-site. ``/readme`` is POST, not
GET, for the same reason: it can trigger a git clone on a cache miss, so it needs the
gate — and a browser only sends `Origin` on a same-origin GET when the mode forces it,
which `fetch()` does not by default, so gating a GET route 403s it for every normal
same-origin caller (confirmed against a real browser: a plain `fetch("/readme?...")`
carries no Origin header at all here, exactly like the updater's own `/status` GET
docstring already notes). POST carries Origin unconditionally, so the gate and the
route agree."""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, HTTPException, Request

from conductor.auth import service as auth_service
from conductor.config import settings

from . import service
from .service import MarketplaceError

router = APIRouter(prefix="/api/plugins/marketplace", tags=["marketplace"])


def _same_origin(request: Request) -> None:
    auth_service.require_origin(request)


def _raise(exc: MarketplaceError) -> NoReturn:
    raise HTTPException(status_code=exc.status_code, detail=exc.message)


@router.get("/index")
async def index() -> dict:
    """Curated index entries (from every configured index URL) plus tracked custom
    repos, each annotated with its install/update status. Network errors on an
    individual index URL are swallowed by ``fetch_index`` — this endpoint never 500s
    because one third-party index.json is unreachable."""
    entries = await service.fetch_index(settings.marketplace_index_url_list)
    merged = await service.merge_with_custom(entries)
    return {"plugins": service.with_install_status(merged)}


@router.get("/installed")
async def installed(force: bool = False) -> dict:
    """Every marketplace-managed plugin, its recorded version, an update-available
    check (network, cached 10 min — pass ``force=true`` to bypass), and the pending
    banner's contents. A name with an unapplied install/update/remove is STAGED but
    not yet RUNNING — for that name ``update_available`` is forced False and
    ``pending_apply`` is True, so the row shows "pending apply" instead of offering
    another update on top of one that hasn't taken effect yet."""
    checks = await service.check_updates(force=force)
    pending_names = service.pending_names()
    rows = []
    for name, info in service.installed_state().items():
        row = {"name": name, **info, **checks["updates"].get(name, {})}
        row["pending_apply"] = name in pending_names
        if row["pending_apply"]:
            row["update_available"] = False
        rows.append(row)
    return {"plugins": rows, "checked_at": checks["checked_at"], "pending": service.pending_apply()}


@router.post("/check_update")
async def check_update(payload: dict, request: Request) -> dict:
    """Body: ``{"name": "<installed plugin name>"}``. A fresh, single-repo tag check —
    what the update dialog calls the instant it opens, bypassing the list-wide 10-min
    cache so a tag pushed moments ago shows up immediately. POST + origin-gated like
    every other route that reaches out over the network on this router, even though
    NAME (not an arbitrary repo string) is the only caller input."""
    _same_origin(request)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="body must carry 'name'")
    try:
        return await service.check_update_for(name)
    except MarketplaceError as exc:
        _raise(exc)


@router.post("/readme")
async def readme(payload: dict, request: Request) -> dict:
    """Body: ``{"repo": "...", "name"?: "..."}``. Pre-install peek at REPO's README,
    for the install confirmation dialog — POST because it can trigger a clone on a
    cache miss (see the module docstring for why GET doesn't work here). Returns
    ``{"markdown": null}`` for a repo with no README (not an error); a clone failure
    comes back as a normal structured HTTPException, never a 500 with a stack, via the
    same ``MarketplaceError`` mapping every other route uses."""
    _same_origin(request)
    repo = str(payload.get("repo") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not repo:
        raise HTTPException(status_code=400, detail="repo is required")
    try:
        markdown = await service.readme_markdown(repo, name or None)
    except MarketplaceError as exc:
        _raise(exc)
    return {"markdown": markdown}


@router.post("/install")
async def install(payload: dict, request: Request) -> dict:
    """Body: ``{"repo": "owner/name-or-url"}`` or ``{"name": "<index entry name>"}``.
    A bare ``name`` is resolved against the current index + custom-repo listing —
    installing IS trusting: the fetched code runs at next restart with conductor's own
    privileges, same trust level as the plugin manager's GitHub import."""
    _same_origin(request)
    repo = str(payload.get("repo") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not repo and not name:
        raise HTTPException(status_code=400, detail="body must carry 'repo' or 'name'")
    if not repo:
        entries = await service.merge_with_custom(await service.fetch_index(settings.marketplace_index_url_list))
        match = next((e for e in entries if e.get("name") == name), None)
        if match is None:
            raise HTTPException(status_code=404, detail=f"{name!r} is not in the index or custom repos")
        repo = match["repo"]
    try:
        return await service.install(repo=repo)
    except MarketplaceError as exc:
        _raise(exc)


@router.post("/update")
async def update(payload: dict, request: Request) -> dict:
    """Body: ``{"name": "<installed plugin name>"}``."""
    _same_origin(request)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="body must carry 'name'")
    try:
        return await service.update(name)
    except MarketplaceError as exc:
        _raise(exc)


@router.post("/remove")
async def remove(payload: dict, request: Request) -> dict:
    """Body: ``{"name": "<installed plugin name>"}``. Deletes the copied local dirs +
    the state entry; the git clone cache under ``~/.conductor/marketplace/repos/`` is
    left in place (a later reinstall/update is then a fetch, not a fresh clone)."""
    _same_origin(request)
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="body must carry 'name'")
    try:
        return service.remove(name)
    except MarketplaceError as exc:
        _raise(exc)


@router.post("/custom_repo")
async def custom_repo(payload: dict, request: Request) -> dict:
    """Body: ``{"url": "owner/name-or-url"}`` — tracks a repo the user pasted in
    alongside index entries, so it shows up in Browse (and can be installed) without
    ever needing to be listed in an index.json."""
    _same_origin(request)
    url = str(payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="body must carry 'url'")
    try:
        return await service.add_custom_repo(url)
    except MarketplaceError as exc:
        _raise(exc)


@router.post("/apply")
async def apply(request: Request) -> dict:
    """Rebuild the frontend (only if something pending touches it) and restart —
    detached, via ``bin/conductorctl restart``'s own ``launchctl kickstart`` mechanism."""
    _same_origin(request)
    try:
        return await service.apply()
    except MarketplaceError as exc:
        _raise(exc)
