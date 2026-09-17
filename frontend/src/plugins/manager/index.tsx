import type { ComponentType } from "react";
import type { PluginTabProps } from "../types";
import { PluginManagerPanel } from "./PluginManagerPanel";

// Plugin-manager FE — a self-contained tab, merged into the registry via
// import.meta.glob. Which is also this plugin's own biggest caveat, stated where it
// belongs: layouts are collected at BUILD time, so an imported plugin's frontend only
// exists after `npm run build` — the reason the panel's restart button offers a rebuild.
export const TAB_LAYOUTS: Record<string, ComponentType<PluginTabProps>> = {
  "plugin-manager": () => <PluginManagerPanel />,
};
