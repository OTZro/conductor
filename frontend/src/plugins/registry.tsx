import type { ComponentType } from "react";
import { IframeLayout } from "./IframeLayout";
import type { LayoutRegistries } from "../kernel/seams";
import { REGISTRY_LAYOUTS, kernel } from "../kernel/seams";
import type { PluginCardProps, PluginMenuProps, PluginTabProps } from "./types";

// The FE layout registry. Only generic PRIMITIVES (iframe) are hardwired here; every
// plugin-specific layout is contributed by a plugin PACKAGE — each
// `src/plugins/<name>/index.{ts,tsx}` (shipped like sandbox/monitor, OR gitignored
// local/) exports the maps it adds, merged below via import.meta.glob. Adding/removing a
// plugin = adding/removing its package; nothing here changes.
export const TAB_LAYOUTS: Record<string, ComponentType<PluginTabProps>> = {
  // generic iframe primitive — any plugin can embed a URL
  iframe: IframeLayout,
};
export const CARD_LAYOUTS: Record<string, ComponentType<PluginCardProps>> = {};
export const MENU_LAYOUTS: Record<string, ComponentType<PluginMenuProps>> = {};

// Merge every plugin package's layouts. A package is `src/plugins/<name>/index.{ts,tsx}`
// exporting the maps it contributes. Absent for a clone → the glob just returns fewer
// entries; the build stays fine.
type LayoutMod = {
  TAB_LAYOUTS?: Record<string, ComponentType<PluginTabProps>>;
  CARD_LAYOUTS?: Record<string, ComponentType<PluginCardProps>>;
  MENU_LAYOUTS?: Record<string, ComponentType<PluginMenuProps>>;
};
const pluginLayouts = import.meta.glob<LayoutMod>("./**/index.{ts,tsx}", { eager: true });
for (const mod of Object.values(pluginLayouts)) {
  Object.assign(TAB_LAYOUTS, mod.TAB_LAYOUTS ?? {});
  Object.assign(CARD_LAYOUTS, mod.CARD_LAYOUTS ?? {});
  Object.assign(MENU_LAYOUTS, mod.MENU_LAYOUTS ?? {});
}

// The merged maps ARE the "registry.layouts" kernel service (M6): the glob
// stays the authoring format, it just feeds the registry — consumers resolve
// `layoutRegistries()` (kernel/seams.ts) instead of importing the maps. The
// provider hands over these same objects, so lookups behave identically.
// The guard shields a dev HMR re-eval from the loud duplicate check. Same
// semantics as kernel/bootstrap.ts: check for the priority-0 row THIS block
// adds, not emptiness, so a plugin package above registering its own provider
// first can never suppress the core registration.
if (!kernel.services.providers(REGISTRY_LAYOUTS).some((p) => p.priority === 0)) {
  kernel.services.register<LayoutRegistries>(
    REGISTRY_LAYOUTS,
    { TAB_LAYOUTS, CARD_LAYOUTS, MENU_LAYOUTS },
    { priority: 0 },
  );
}
