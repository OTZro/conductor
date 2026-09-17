"""Sandbox plugin — the OrbStack VM inventory, as the first "double-sided" plugin: it
contributes both a tab (grouped host tiles) and a card-detail widget (the VM whose name
matches this card's ticket). Reuses the existing actions.sandboxes data layer."""

from __future__ import annotations

from . import inventory as sb
from conductor.config import settings
from ..base import CardWidgetSpec, Plugin, TabSpec


async def _tab_data() -> list[dict]:
    """Per-host inventory for the tab. Adds the host display name so the FE needn't
    special-case the local machine (host=null)."""
    out = await sb.all_sandboxes()
    for d in out:
        d["name"] = d.get("host") or settings.local_host_name
    return out


def _ticket_keys(ctx: dict) -> set[str]:
    """The jira ticket key(s) a card is about: its own key (jira cards) + any jira links."""
    keys: set[str] = set()
    if ctx.get("origin") == "jira" and ctx.get("external_id"):
        keys.add(str(ctx["external_id"]).upper())
    for link in ctx.get("links") or []:
        if link.get("kind") == "jira" and link.get("ref"):
            keys.add(str(link["ref"]).upper())
    return keys


async def _card_data(ctx: dict) -> dict | None:
    """The OrbStack VM named after this card's ticket (PROJ-9406 ↔ proj-9406), or None if
    no ticket key or no matching VM."""
    keys = _ticket_keys(ctx)
    if not keys:
        return None
    for host in await sb.all_sandboxes():
        for vm in host.get("vms", []):
            if (vm.get("name") or "").upper() in keys:
                return {
                    "host": host.get("host"),
                    "hostName": host.get("host") or settings.local_host_name,
                    "vm": vm,
                }
    return None


PLUGIN = Plugin(
    id="sandbox",
    label="Sbx",
    icon="▤",
    tab=TabSpec(layout="sandbox-hosts", provider=_tab_data),
    card_widget=CardWidgetSpec(title="Sandbox", layout="sandbox-card", provider=_card_data),
)
