"""Plugin manager routes — mounted at ``/api/plugins/manage``.

Everything here sits behind conductor's own auth (no HookSpec, so no exemption): every
route either changes what code runs at next boot or reads/writes plugin source trees,
which is owner-only by definition.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import io
import json
import logging
import re
import shutil
import tarfile
import time
import zipfile
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from .. import MODULES, RENDER_SLOTS, disabled_modules, order_overrides, write_plugins_conf

log = logging.getLogger("conductor.plugins.manager")

router = APIRouter(prefix="/api/plugins/manage", tags=["plugin-manager"])

_ROOT = Path(__file__).resolve().parents[4]  # …/backend/conductor/plugins/manager/router.py → repo root
_BE_LOCAL = _ROOT / "backend" / "conductor" / "plugins" / "local"
_FE_LOCAL = _ROOT / "frontend" / "src" / "plugins" / "local"
_FE_SHIPPED = _ROOT / "frontend" / "src" / "plugins"
_TRASH = Path.home() / ".conductor" / "disabled-plugins"
_SOURCES = Path.home() / ".conductor" / "plugins-sources.json"
_APPLY_LOG = Path.home() / ".conductor" / "logs" / "plugin-apply.log"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _dirs(root: Path) -> set[str]:
    if not root.is_dir():
        return set()
    return {
        d.name for d in root.iterdir()
        if d.is_dir() and not d.name.startswith(("_", ".")) and d.name != "local"
    }


# What this boot was born with. `pending restart` is any drift from these — a toggled
# switch, an imported or deleted directory — because none of it takes effect until the
# next boot. Snapshotted at import (i.e. at boot) on purpose.
_BOOT_DISABLED = disabled_modules()
_BOOT_BE = _dirs(_BE_LOCAL)
_BOOT_FE = _dirs(_FE_LOCAL)


def _sources() -> dict:
    try:
        raw = json.loads(_SOURCES.read_text())
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")


_DESC_CACHE: dict[str, str] = {}


def _plugin_dirs(module: str, source: str) -> tuple[Path, Path]:
    be_root = _BE_LOCAL if source == "local" else _ROOT / "backend" / "conductor" / "plugins"
    fe_root = _FE_LOCAL if source == "local" else _FE_SHIPPED
    return be_root / module, fe_root / module


def _manifest(module: str, source: str) -> dict:
    """The plugin's own conductor-plugin.json, living IN its directory — so it travels
    with every export/import untouched (the dir is zipped whole). Authoritative for
    the description and may declare the plugin's home repo (``github``)."""
    for d in _plugin_dirs(module, source):
        f = d / "conductor-plugin.json"
        if f.is_file():
            try:
                raw = json.loads(f.read_text())
                return raw if isinstance(raw, dict) else {}
            except (OSError, ValueError):
                return {}
    return {}


def _description(module: str, source: str) -> str:
    """Manifest first — the author's curated words, in whatever language they chose.
    Fallbacks are the backend module docstring (read via ast so nothing executes,
    which is what lets a DISABLED or broken plugin still describe itself) and, for
    frontend-only plugins, the leading // comment block of index.tsx."""
    key = f"{source}:{module}"
    if key in _DESC_CACHE:
        return _DESC_CACHE[key]
    manifest_desc = str(_manifest(module, source).get("description") or "").strip()
    if manifest_desc:
        _DESC_CACHE[key] = manifest_desc[:600]
        return _DESC_CACHE[key]
    be_root = _BE_LOCAL if source == "local" else _ROOT / "backend" / "conductor" / "plugins"
    desc = ""
    for cand in (be_root / module / "__init__.py", be_root / f"{module}.py"):
        if cand.is_file():
            try:
                desc = ast.get_docstring(ast.parse(cand.read_text())) or ""
            except (OSError, SyntaxError):
                desc = ""
            break
    if not desc:
        fe = (_FE_LOCAL if source == "local" else _FE_SHIPPED) / module / "index.tsx"
        if fe.is_file():
            block: list[str] = []
            for ln in fe.read_text().splitlines()[:40]:
                t = ln.strip()
                if t.startswith("//"):
                    block.append(t.lstrip("/").strip())
                elif block:
                    break
            desc = " ".join(block)
    desc = desc.split("\n\n")[0].strip()[:600]  # first paragraph is the summary
    _DESC_CACHE[key] = desc
    return desc


def _shipped_modules() -> set[str]:
    return {r["module"] for r in MODULES if r["source"] == "shipped"}


def _slot_order_map(slot: str) -> dict[str, int]:
    """module -> effective order, for every module whose LOADED plugin declares
    ``slot`` (``getattr(plugin, slot) is not None``) — override first, else the
    plugin's own base ``Plugin.order``. Restricted to loaded plugins because slot
    membership is only knowable post-import: a module this boot never loaded (disabled
    before boot, or broken) cannot be placed in a slot's order — it isn't known to
    contribute to it at all. This is the domain both ``/slots`` and ``/reorder`` work
    in, one slot at a time (a plugin's nav position, card-widget position, and
    menu-bar position are independent sequences)."""
    # deferred: this module is imported BY discover() (it's the manager plugin's own
    # ROUTER) while conductor.plugins is still initializing, before its own PLUGINS
    # name exists — a top-level `from .. import PLUGINS` would fail with exactly the
    # circular-import error this sidesteps. By the time any request calls this
    # function, `..` is fully initialized.
    from .. import PLUGINS

    overrides = order_overrides().get(slot, {})
    module_by_id = {r["id"]: r["module"] for r in MODULES if r.get("id")}
    out: dict[str, int] = {}
    for p in PLUGINS.values():
        if getattr(p, slot, None) is None:
            continue
        module = module_by_id.get(p.id)
        if module is None:
            continue
        out[module] = overrides.get(module, p.order)
    return out


@router.get("/list")
async def list_plugins() -> dict:
    """Every plugin module, loaded or not, with what would change on restart.

    Built by overlaying the boot-time MODULES registry onto a LIVE filesystem scan, so
    a plugin imported two minutes ago shows up (as pending) even though this process
    has never loaded it, and one just deleted shows as pending removal."""
    disabled = disabled_modules()
    sources = _sources()
    by_module = {r["module"]: r for r in MODULES}

    be_now, fe_now = _dirs(_BE_LOCAL), _dirs(_FE_LOCAL)
    rows: list[dict] = []

    for r in MODULES:
        fe_dir = (_FE_LOCAL if r["source"] == "local" else _FE_SHIPPED) / r["module"]
        rows.append({
            **r,
            "enabled": r["module"] not in disabled,  # the switch's CURRENT position
            "enabled_at_boot": r["enabled"],  # what this process actually honoured
            "has_frontend": fe_dir.is_dir(),
            "github": (sources.get(r["module"]) or {}).get("repo")
            or _manifest(r["module"], r["source"]).get("github"),
            "description": _description(r["module"], r["source"]),
        })

    # directories that exist NOW but were not part of this boot: freshly imported
    # backend plugins, and frontend-only plugins (which never appear in MODULES —
    # they have no backend module to import)
    for name in sorted((be_now | fe_now) - set(by_module)):
        rows.append({
            "module": name, "source": "local", "id": None, "label": None,
            "enabled": name not in disabled, "enabled_at_boot": None,
            "loaded": False, "error": None,
            "has_frontend": name in fe_now, "frontend_only": name not in be_now,
            "github": (sources.get(name) or {}).get("repo") or _manifest(name, "local").get("github"),
            "description": _description(name, "local"),
        })

    pending = (
        disabled != _BOOT_DISABLED or be_now != _BOOT_BE or fe_now != _BOOT_FE
    )
    return {"plugins": rows, "pending_restart": pending}


@router.post("/toggle")
async def toggle(payload: dict) -> dict:
    module = str(payload.get("module") or "")
    enabled = bool(payload.get("enabled"))
    if not _NAME_RE.match(module):
        raise HTTPException(status_code=400, detail="bad module name")
    if module == "manager" and not enabled:
        # the switchboard must not be able to switch ITSELF off from the UI — the only
        # way back would be hand-editing ~/.conductor/plugins.json, which is exactly
        # the situation this plugin exists to remove
        raise HTTPException(status_code=400, detail="the plugin manager cannot disable itself")
    disabled = disabled_modules()
    if enabled:
        disabled.discard(module)
    else:
        disabled.add(module)
    write_plugins_conf({"disabled": sorted(disabled)})  # merge — must not drop "order"
    return {"ok": True, "module": module, "enabled": enabled, "restart_needed": True}


@router.get("/slots")
async def list_slots() -> dict:
    """Per-render-slot plugin lists, in current effective order — what the manager
    panel's "版面排序" section renders: one sub-block per slot, each row orderable
    within that slot alone. Built from PLUGINS (this boot's live manifest), not the
    MODULES directory scan, because slot membership (does this plugin contribute a
    tab/card-widget/menu-bar?) is only knowable once imported. A plugin toggled off
    AFTER this boot loaded it still appears (grayed via "enabled") — the switchboard's
    "disabled" flag has no live effect on an already-imported plugin until restart,
    same as everywhere else in this file; a module that was disabled (or broken)
    BEFORE this boot never got a Plugin instance and so cannot appear in any slot."""
    disabled = disabled_modules()
    module_by_id = {r["id"]: r["module"] for r in MODULES if r.get("id")}
    from .. import PLUGINS  # deferred — see _slot_order_map

    slots: dict[str, list[dict]] = {}
    for slot in RENDER_SLOTS:
        eff = _slot_order_map(slot)
        rows = [
            {
                "module": module_by_id[p.id], "id": p.id, "label": p.label,
                "enabled": module_by_id[p.id] not in disabled,
                "order": eff[module_by_id[p.id]],
            }
            for p in PLUGINS.values()
            if getattr(p, slot, None) is not None and p.id in module_by_id
        ]
        rows.sort(key=lambda r: (r["order"], r["module"]))
        slots[slot] = rows
    return {"slots": slots}


@router.post("/reorder")
async def reorder(payload: dict) -> dict:
    """Bulk, per-slot order commit — the manager panel batches every local drag/arrow
    move (no per-move network call) and fires ONE of these when the user hits
    「套用並重新整理」. Body: ``{"order": {slot: [module, ...]}}`` — one fully-ordered
    module list per DIRTY slot; list position becomes dense order (10, 20, 30, …), so
    the panel just sends its current on-screen order and never computes positions
    itself.

    Validation is strict, not best-effort: an unknown slot, a non-list value, or a
    module list that doesn't match EXACTLY the slot's current known plugin set (missing
    one, carrying an unknown name, or a duplicate) is a 400 — a stale panel (a plugin
    was toggled/removed since the panel's last fetch) must not silently commit a
    partial or corrupted order. "Known" is the same live-manifest domain
    ``_slot_order_map`` and ``/slots`` use — a module not currently declaring that slot
    cannot appear in it.

    Merges into the existing per-slot overrides so a payload naming only SOME dirty
    slots leaves every other slot's positions untouched (order_overrides() read first,
    only the named slots replaced, then one whole-file write — the same shallow-merge
    pitfall ``write_plugins_conf`` exists to prevent, one level deeper). No restart
    needed: the manifest applies this file at response time, so the panel's own next
    /slots call (and the FE's next manifest fetch, after its own reload) already see
    it."""
    order = payload.get("order")
    if not isinstance(order, dict) or not order:
        raise HTTPException(status_code=400, detail="body must be {'order': {slot: [module, ...]}}")

    all_orders = order_overrides()
    result: dict[str, dict[str, int]] = {}
    for slot, modules in order.items():
        slot = str(slot)
        if slot not in RENDER_SLOTS:
            raise HTTPException(status_code=400, detail=f"unknown slot {slot!r}")
        if not isinstance(modules, list) or not all(isinstance(m, str) for m in modules):
            raise HTTPException(status_code=400, detail=f"{slot!r} must be a list of module names")
        known = set(_slot_order_map(slot))
        given = set(modules)
        if given != known or len(modules) != len(given):
            raise HTTPException(
                status_code=400,
                detail=f"{slot!r}'s module list doesn't match its current plugins (stale panel state?)",
            )
        dense = {m: (i + 1) * 10 for i, m in enumerate(modules)}
        all_orders[slot] = dense
        result[slot] = dense

    write_plugins_conf({"order": all_orders})
    return {"ok": True, "order": result}


@router.get("/export/{module}")
async def export_plugin(module: str) -> Response:
    """One zip carrying the plugin's two halves — the same layout import accepts, so an
    export re-imports on another conductor untouched. Local only: a shipped plugin's
    distribution channel is the repo itself."""
    if not _NAME_RE.match(module):
        raise HTTPException(status_code=400, detail="bad module name")
    be, fe = _BE_LOCAL / module, _FE_LOCAL / module
    if not be.is_dir() and not fe.is_dir():
        raise HTTPException(status_code=404, detail="no such local plugin")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, top in ((be, "backend"), (fe, "frontend")):
            if not root.is_dir():
                continue
            for f in sorted(root.rglob("*")):
                if f.is_dir() or "__pycache__" in f.parts or f.name.endswith(".pyc"):
                    continue
                z.write(f, f"{top}/{module}/{f.relative_to(root)}")
        # The root manifest is the plugin's OWN manifest plus export metadata — merged,
        # not a separate stub, because whoever opens the zip opens THIS file and should
        # find the whole story there (the copy inside backend/<n>/ is what import
        # actually reads, but two manifests that disagree is a bug report waiting to
        # be filed). Recorded provenance wins over a manifest-declared home repo.
        src = _sources().get(module) or {}
        base = _manifest(module, "local")
        z.writestr(
            "conductor-plugin.json",
            json.dumps({
                **base,
                "name": module,
                "exported_at": int(time.time()),
                "github": src.get("repo") or base.get("github"),
                "sha": src.get("sha"),
            }, ensure_ascii=False, indent=2),
        )
    return Response(
        content=buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="conductor-plugin-{module}.zip"'},
    )


def _install_tree(tree: Path, *, overwrite: bool, provenance: dict | None) -> dict:
    """Move an extracted plugin tree (backend/<n>, frontend/<n>) into the local roots.

    The two guards are the point of this function: a SHIPPED module name is refused
    outright (a local dir would shadow it in confusing ways), and an existing local
    plugin is only replaced when the caller said so — and even then the old version is
    moved to the trash dir first, never destroyed."""
    be_names = _dirs(tree / "backend")
    fe_names = _dirs(tree / "frontend")
    names = be_names | fe_names
    if not names:
        raise HTTPException(status_code=400, detail="archive carries no backend/<name> or frontend/<name>")
    if len(names) > 1:
        raise HTTPException(status_code=400, detail=f"one plugin per archive (found {sorted(names)})")
    name = names.pop()
    if not _NAME_RE.match(name):
        raise HTTPException(status_code=400, detail=f"bad plugin name {name!r}")
    if name in _shipped_modules():
        raise HTTPException(status_code=400, detail=f"{name!r} is a shipped plugin — a local copy would shadow it")

    existing = [(r, r / name) for r in (_BE_LOCAL, _FE_LOCAL) if (r / name).is_dir()]
    if existing and not overwrite:
        raise HTTPException(status_code=409, detail=f"local plugin {name!r} already exists (pass overwrite)")
    if existing:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for _root, old in existing:
            keep = _TRASH / f"{name}-{stamp}" / ("backend" if _root is _BE_LOCAL else "frontend")
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(old), str(keep))

    installed = []
    for sub, dest_root in (("backend", _BE_LOCAL), ("frontend", _FE_LOCAL)):
        src = tree / sub / name
        if src.is_dir():
            dest_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dest_root / name)
            installed.append(sub)

    if not provenance:
        # no caller-supplied provenance (a bare zip): the plugin may still declare its
        # own home repo in the manifest INSIDE its directory — honour it, so authorship
        # travels with the code and the recipient gets the update path regardless of
        # how the archive reached them
        for sub in ("backend", "frontend"):
            mf = tree / sub / name / "conductor-plugin.json"
            if mf.is_file():
                try:
                    inner = json.loads(mf.read_text())
                    if isinstance(inner, dict) and inner.get("github"):
                        provenance = {
                            "repo": str(inner["github"]), "ref": None,
                            "sha": None, "via": "zip-manifest", "fetched_at": int(time.time()),
                        }
                except (OSError, ValueError):
                    pass
                break
    if provenance:
        _write_json(_SOURCES, {**_sources(), name: provenance})
    return {"module": name, "installed": installed, "replaced": bool(existing)}


def _safe_extract_zip(data: bytes, dest: Path) -> None:
    root = dest.resolve()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        for info in z.infolist():
            # is_relative_to, not startswith: a prefix check has no path-separator
            # boundary, so "/tmp/import-x-evil" passes a "/tmp/import-x" prefix test
            if not (root / info.filename).resolve().is_relative_to(root):
                raise HTTPException(status_code=400, detail="archive path escapes its root")
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                # a symlink materialised into the tree points wherever it likes; plugin
                # code has no business carrying one
                raise HTTPException(status_code=400, detail="archive contains a symlink")
        z.extractall(dest)


@router.post("/import")
async def import_zip(payload: dict) -> dict:
    """Body: {"data_b64", "overwrite"?}. Base64 rather than multipart so this needs no
    new dependency; a plugin zip is a few hundred KB. Installing IS trusting: the code
    will run at next boot with conductor's own privileges — same trust level as the
    updater's git pull, stated here so nobody mistakes import for a sandbox."""
    try:
        data = base64.b64decode(str(payload.get("data_b64") or ""), validate=True)
    except Exception:
        raise HTTPException(status_code=400, detail="data_b64 is not valid base64")
    if not data:
        raise HTTPException(status_code=400, detail="empty archive")

    tmp = Path.home() / ".conductor" / "tmp" / f"import-{int(time.time()*1000)}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        _safe_extract_zip(data, tmp)
        # a zip is a dead end ONLY when it carries no identity. One exported from a
        # github-sourced plugin names its repo in the manifest — honour it, so the
        # recipient inherits the update path instead of an orphan copy.
        provenance = None
        manifest_path = tmp / "conductor-plugin.json"
        if manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text())
                if manifest.get("github"):
                    provenance = {
                        "repo": str(manifest["github"]), "ref": None,
                        "sha": manifest.get("sha"), "via": "zip",
                        "fetched_at": int(time.time()),
                    }
            except (ValueError, TypeError):
                pass
        result = _install_tree(tmp, overwrite=bool(payload.get("overwrite")), provenance=provenance)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"ok": True, **result, "restart_needed": True}


@router.post("/import-github")
async def import_github(payload: dict) -> dict:
    """Body: {"repo": "owner/name", "ref"?, "overwrite"?}. Fetches the repo tarball via
    ``gh api`` — the gh auth already on this machine is the whole access story, private
    org repos included — and records the repo as the module's provenance, which is what
    the update button re-fetches later. The repo's layout must be the export layout
    (backend/<name>, frontend/<name> at the top)."""
    repo = str(payload.get("repo") or "").strip()
    ref = str(payload.get("ref") or "").strip()
    if not re.match(r"^[\w.-]+/[\w.-]+$", repo):
        raise HTTPException(status_code=400, detail="repo must be owner/name")

    sha = await _remote_sha(repo, ref)  # before the tarball: the version we are about to install
    url = f"repos/{repo}/tarball" + (f"/{ref}" if ref else "")
    proc = await asyncio.create_subprocess_exec(
        "gh", "api", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        # wait_for cancels the await but leaves gh running — kill and reap it, or every
        # timed-out import leaks a process (the updater's fetch learned this same lesson)
        proc.kill()
        await proc.wait()
        raise HTTPException(status_code=502, detail="gh api timed out fetching the tarball")
    if proc.returncode != 0 or not out:
        raise HTTPException(status_code=502, detail=f"gh api failed: {err.decode()[:300]}")

    tmp = Path.home() / ".conductor" / "tmp" / f"gh-{int(time.time()*1000)}"
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(out), mode="r:gz") as t:
            # filter="data" is the stdlib's own answer here: it rejects absolute paths,
            # traversal AND link members — a pre-check loop cannot account for symlinks
            # that extractall would happily materialise
            t.extractall(tmp, filter="data")
        # a GitHub tarball wraps everything in one "owner-repo-sha/" dir — unwrap it
        entries = [d for d in tmp.iterdir() if d.is_dir()]
        tree = entries[0] if len(entries) == 1 and not (tmp / "backend").is_dir() else tmp
        result = _install_tree(
            tree,
            overwrite=bool(payload.get("overwrite")),
            provenance={"repo": repo, "ref": ref or None, "sha": sha, "fetched_at": int(time.time())},
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return {"ok": True, **result, "restart_needed": True}


async def _gh(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "gh", "api", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()  # see import_github: an abandoned await is not a dead process
        await proc.wait()
        raise RuntimeError("gh timed out")
    if proc.returncode != 0:
        raise RuntimeError(err.decode()[:200] or "gh failed")
    return out.decode().strip()


async def _remote_sha(repo: str, ref: str) -> str | None:
    """HEAD of the repo (or of ``ref``) — the version identity everything compares on.
    None on failure rather than raising: an unreachable repo must not break an import
    (you lose the version stamp, not the plugin)."""
    try:
        if not ref:
            ref = await _gh(f"repos/{repo}", "--jq", ".default_branch")
        return await _gh(f"repos/{repo}/commits/{ref}", "--jq", ".sha") or None
    except Exception as exc:  # noqa: BLE001
        log.info("[manager] remote sha for %s failed: %s", repo, exc)
        return None


_UPD_CACHE: dict = {"ts": 0.0, "data": None}


@router.get("/check-updates")
async def check_updates(force: bool = False) -> dict:
    """Per github-sourced module: is the repo ahead of what is installed? This is the
    badge that makes the update button informed instead of blind — without it "更新"
    is a ritual you perform on faith. Cached 10 minutes; separate from /list so the
    plugin table never waits on the network."""
    now = time.monotonic()
    if not force and _UPD_CACHE["data"] is not None and now - _UPD_CACHE["ts"] < 600:
        return _UPD_CACHE["data"]
    out: dict[str, dict] = {}

    async def one(mod: str, info: dict) -> None:
        remote = await _remote_sha(info["repo"], info.get("ref") or "")
        local = info.get("sha")
        out[mod] = {
            "local_sha": local, "remote_sha": remote,
            # unknown local version + reachable repo → offer the update; taking it is
            # also what STAMPS the version, ending the unknown state
            "update_available": bool(remote) and remote != local,
        }

    srcs = {m: i for m, i in _sources().items() if isinstance(i, dict) and i.get("repo")}
    await asyncio.gather(*(one(m, i) for m, i in srcs.items()))
    result = {"updates": out, "checked_at": int(time.time())}
    _UPD_CACHE["ts"], _UPD_CACHE["data"] = now, result
    return result


@router.post("/delete")
async def delete_plugin(payload: dict) -> dict:
    """Remove a LOCAL plugin — by moving it to ~/.conductor/disabled-plugins/, never by
    unlinking. Everything this endpoint does is reversible by moving the dir back."""
    module = str(payload.get("module") or "")
    if not _NAME_RE.match(module):
        raise HTTPException(status_code=400, detail="bad module name")
    moved = []
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for root, sub in ((_BE_LOCAL, "backend"), (_FE_LOCAL, "frontend")):
        src = root / module
        if src.is_dir():
            keep = _TRASH / f"{module}-{stamp}" / sub
            keep.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(keep))
            moved.append(sub)
    if not moved:
        raise HTTPException(status_code=404, detail="no such local plugin")
    srcs = _sources()
    if module in srcs:
        srcs.pop(module)
        _write_json(_SOURCES, srcs)
    return {"ok": True, "module": module, "moved_to": str(_TRASH / f"{module}-{stamp}"), "restart_needed": True}


@router.post("/apply")
async def apply(payload: dict) -> dict:
    """Restart conductor, optionally rebuilding the frontend first. Detached in its own
    session (the updater's pattern) so the restart it triggers cannot kill it mid-way.
    The rebuild matters whenever a plugin with frontend files changed: layouts are
    collected at BUILD time (import.meta.glob), so without it an imported panel simply
    does not exist in the served bundle."""
    rebuild = bool(payload.get("rebuild"))
    build = ""
    if rebuild:
        build = (
            'command -v npm >/dev/null 2>&1 || { for d in "$HOME"/.nvm/versions/node/*/bin; do PATH="$d:$PATH"; done; }\n'
            "npm --prefix frontend run build || echo '!! frontend build failed — restarting on the old bundle'\n"
        )
    script = (
        f'exec >> "{_APPLY_LOG}" 2>&1\n'
        f'echo "== plugin apply $(date) rebuild={rebuild}"\n'
        f'cd "{_ROOT}"\n'
        f"{build}"
        f'exec "{_ROOT}/bin/conductorctl" restart\n'
    )
    _APPLY_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        await asyncio.create_subprocess_exec(
            "/bin/bash", "-c", script,
            start_new_session=True,  # survives the backend it is about to restart
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"couldn't launch apply: {exc}")
    return {"ok": True, "restarting": True, "rebuild": rebuild}
