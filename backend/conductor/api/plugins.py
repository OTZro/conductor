from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ..plugins import MODULES, PLUGINS, RENDER_SLOTS, order_overrides, runtime

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


@router.get("/manifest")
async def manifest() -> list[dict]:
    """Every discovered plugin's declaration — what tabs / card-widgets to render. The
    FE reads this to build its nav + card-detail slots dynamically.

The user's plugin-manager order overrides (per RENDER SLOT — a plugin's nav
    position, card-widget position, and menu-bar position are independent) are applied
    HERE, at response time, not on the frozen Plugin dataclass — read fresh per request
    (cheap: a few bytes of JSON) so a reorder in the panel is visible on the very next
    fetch, no backend restart required. Each manifest entry gets an ``orders`` map
    (slot -> effective order, defaulting to the plugin's own base ``order``); each FE
    slot consumer sorts by ITS OWN key (PluginMenuBar by ``orders.menu_bar``, the nav by
    ``orders.tab``, PluginCardWidgets by ``orders.card_widget``) rather than relying on
    this list's own array order, since one array order cannot serve three independent
    sequences at once. The top-level ``order``/id sort here just keeps the response
    stable across requests.

    Deliberately still assembled from ``PLUGINS``, not the kernel slot registries:
    the manifest is whole-plugin identity (id / label / icon / order) the per-slot
    rows don't carry, and the registries are wiped by any test's ``reset_kernel()``
    while ``PLUGINS`` survives — the M2 decision to keep this response byte-stable
    stands (the ``orders`` graft reads conf, not registries)."""
    overrides = order_overrides()  # {slot: {module: order}}
    module_by_id = {r["id"]: r["module"] for r in MODULES if r.get("id")}
    manifests = []
    for p in PLUGINS.values():
        m = p.manifest()
        module = module_by_id.get(p.id)
        m["orders"] = {
            slot: overrides.get(slot, {}).get(module, p.order) if module is not None else p.order
            for slot in RENDER_SLOTS
        }
        manifests.append(m)
    manifests.sort(key=lambda m: (m["order"], m["id"]))
    return manifests


@router.get("/{plugin_id}/tab")
async def tab(plugin_id: str):
    """A plugin tab's data (shape defined by its layout). Self-contained tabs (no
    provider) return nothing — their layout talks to its own router. Resolved via
    the kernel (``slot_spec``): PLUGINS read-through for discovery-authored
    plugins, direct spec for kernel-native registrants."""
    spec = runtime.slot_spec("tab", plugin_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="no such plugin tab")
    if spec.provider is None:
        return {}
    return await spec.provider()


@router.post("/{plugin_id}/card")
async def card(plugin_id: str, ctx: dict) -> dict:
    """A plugin card-widget's data for one card context; ``{data: null}`` means the
    widget shows nothing for this card. ctx carries origin / external_id / links.
    Resolved via the kernel (``slot_spec``), same read-through as the tab route."""
    spec = runtime.slot_spec("card_widget", plugin_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="no such plugin card widget")
    return {"data": await spec.provider(ctx)}
