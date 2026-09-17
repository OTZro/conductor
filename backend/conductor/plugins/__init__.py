"""Conductor plugins — self-contained feature modules discovered at import.

A plugin module exposes a module-level ``PLUGIN = Plugin(...)`` declaring its
contributions (a tab / card-widget / menu-bar). It MAY also expose ``ROUTER`` (a
FastAPI ``APIRouter``) which the app mounts — that's how a self-contained plugin brings
its own backend endpoints.

Two search roots: the shipped plugins here, and a **gitignored** ``local/`` sub-package
— a personal "drop your own plugin here" dir that never enters version control. Absent
``local/`` is fine (other clones just get the shipped plugins). This is the seam that
lets a feature live entirely outside the repo.
"""

from __future__ import annotations

import json
import logging
import os
from importlib import import_module
from pathlib import Path
from pkgutil import iter_modules

from . import runtime
from .base import (
    RENDER_SLOTS,
    CardWidgetSpec,
    HookSpec,
    LaneChangeSpec,
    LinkEnricherSpec,
    LinkMatcherSpec,
    MenuBarSpec,
    NotifySpec,
    Plugin,
    PollSpec,
    StageSpec,
    StateProbeSpec,
    TabSpec,
)

log = logging.getLogger("conductor.plugins")

__all__ = [
    "PLUGINS",
    "PLUGIN_ROUTERS",
    "MODULES",
    "RENDER_SLOTS",
    "disabled_modules",
    "order_overrides",
    "write_plugins_conf",
    "CardWidgetSpec",
    "HookSpec",
    "LaneChangeSpec",
    "LinkEnricherSpec",
    "LinkMatcherSpec",
    "MenuBarSpec",
    "NotifySpec",
    "Plugin",
    "PollSpec",
    "StageSpec",
    "StateProbeSpec",
    "TabSpec",
    "discover",
]


# User's plugin switchboard (~/.conductor/plugins.json, {"disabled": [...], "order":
# {slot: {module: int}}}). "disabled" is keyed by MODULE (directory) name, not plugin
# id, deliberately: the id only exists after a successful import, and the plugins most
# worth disabling are exactly the ones that fail to import. A disabled module is never
# imported at all — its code cannot run, which is what "disabled" has to mean for
# something that executes at startup. "order" is PER RENDER SLOT (see base.RENDER_SLOTS)
# because a plugin's position in the nav is a wholly separate question from its position
# in the menu bar or among card widgets — one flat order could not answer all three.
_PLUGINS_CONF = Path.home() / ".conductor" / "plugins.json"


def _read_conf() -> dict:
    """The whole switchboard file, shape-checked to a dict — never raises. Both
    ``disabled_modules()`` and ``order_overrides()`` read through this, and
    ``write_plugins_conf()`` writes through it, so neither key ever clobbers the other."""
    try:
        raw = json.loads(_PLUGINS_CONF.read_text())
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def disabled_modules() -> set[str]:
    raw = _read_conf()
    # shape-checked, not just parsed: this runs at IMPORT, so a hand-edited file that
    # holds a list (or {"disabled": "name"}) would otherwise AttributeError/iterate-chars
    # and take the whole backend down at startup — the failure mode a disable switch
    # exists to prevent, not cause
    items = raw.get("disabled")
    return {str(m) for m in items} if isinstance(items, list) else set()


def order_overrides() -> dict[str, dict[str, int]]:
    """slot -> module -> user-set display order, read fresh per call (this file is a
    few bytes; a cache would be optimizing a cost that doesn't exist). Shape-checked
    the same way as ``disabled_modules()``, two levels deep: an unknown top-level shape,
    a non-dict per-slot value, or a non-int per-module value is dropped rather than
    raising — serving the manifest must never 500 because of a malformed switchboard
    file. This also quietly retires an EARLIER, never-released flat {module: int} shape
    from before per-slot ordering existed: its values are ints, not dicts, so the
    ``isinstance(mapping, dict)`` check below drops it whole rather than misreading it
    as one slot's overrides."""
    raw = _read_conf().get("order")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, int]] = {}
    for slot, mapping in raw.items():
        if not isinstance(mapping, dict):
            continue
        slot_out = {
            str(k): v for k, v in mapping.items()
            if isinstance(v, int) and not isinstance(v, bool)  # bool is an int subclass
        }
        if slot_out:
            out[str(slot)] = slot_out
    return out


def write_plugins_conf(patch: dict) -> None:
    """Merge ``patch`` into the on-disk switchboard and write it back whole — so a
    caller writing "disabled" never clobbers a previously-written "order" and vice
    versa (a plain overwrite here is exactly the bug this function exists to prevent)."""
    conf = {**_read_conf(), **patch}
    _PLUGINS_CONF.parent.mkdir(parents=True, exist_ok=True)
    _PLUGINS_CONF.write_text(json.dumps(conf, indent=2, ensure_ascii=False) + "\n")


# One row per module directory found at startup, loaded or not — what the plugin
# manager lists. `source` is which root it came from; `error` keeps a broken plugin
# visible (with its reason) instead of silently absent.
MODULES: list[dict] = []


def _scan(
    search_path: str, pkg: str, plugins: dict[str, Plugin], routers: list,
    source: str, disabled: set[str],
) -> None:
    """Import every non-underscore module found at ``search_path`` (dotted ``pkg``) and
    collect its ``PLUGIN`` / ``ROUTER``. A broken module is logged and skipped, never
    fatal — a broken local plugin degrades gracefully instead of taking the app down."""
    for mod in iter_modules([search_path]):
        if mod.name.startswith(("_", "base")) or mod.name == "local":
            continue
        row = {
            "module": mod.name, "source": source, "id": None, "label": None,
            "enabled": mod.name not in disabled, "loaded": False, "error": None,
        }
        MODULES.append(row)
        if not row["enabled"]:
            log.info("[plugins] %s disabled via plugins.json — not imported", mod.name)
            continue
        try:
            m = import_module(f"{pkg}.{mod.name}")
        except Exception as exc:  # noqa: BLE001
            row["error"] = str(exc)[:300]
            log.warning("[plugins] skip %s: %s", mod.name, exc)
            continue
        plugin = getattr(m, "PLUGIN", None)
        if isinstance(plugin, Plugin):
            plugins[plugin.id] = plugin
            row["loaded"], row["id"], row["label"] = True, plugin.id, plugin.label
            # mirror the declaration into the kernel as a reversible scope (M2
            # shim; since M3 consumers resolve through it with a PLUGINS
            # read-through). A failure here must degrade exactly like any other
            # plugin fault — logged, never fatal, and the legacy surface above
            # stays intact either way.
            row["kernel_registered"] = True
            try:
                runtime.register_plugin(mod.name, plugin)
            except Exception as exc:  # noqa: BLE001
                # loaded=True + kernel_registered=False is a DIVERGENCE worth
                # surfacing: the kernel scope is missing, so scope-based features
                # (teardown, introspection) skip this plugin. spec_rows() still
                # SERVES it via its PLUGINS read-through — deliberately, a half
                # registered plugin should degrade to legacy behavior, not vanish —
                # but that safety net is exactly why the flag must be visible in
                # /api/plugins/manage/list rather than buried in one log line.
                row["kernel_registered"] = False
                log.warning("[plugins] kernel registration failed for %s: %s", mod.name, exc)
        router = getattr(m, "ROUTER", None)
        if router is not None:
            routers.append(router)


def discover() -> tuple[dict[str, Plugin], list]:
    """(plugins, routers) from the shipped dir + the gitignored ``local/`` dir."""
    plugins: dict[str, Plugin] = {}
    routers: list = []
    disabled = disabled_modules()
    _scan(__path__[0], __name__, plugins, routers, "shipped", disabled)
    local = os.path.join(__path__[0], "local")
    if os.path.isdir(local):
        _scan(local, f"{__name__}.local", plugins, routers, "local", disabled)
    return plugins, routers


PLUGINS, PLUGIN_ROUTERS = discover()
