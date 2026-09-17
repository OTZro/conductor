import type { ComponentType } from "react";
import type { PluginTabProps } from "../types";
import { MarketplacePanel } from "./MarketplacePanel";

// Plugin-marketplace FE — a self-contained tab, merged into the registry via
// import.meta.glob (see registry.tsx). Browse an index.json (+ custom repos), install
// at the latest semver git tag, update, remove — everything backed by
// /api/plugins/marketplace/*; nothing here is wired into core beyond this one map.
export const TAB_LAYOUTS: Record<string, ComponentType<PluginTabProps>> = {
  "plugin-marketplace": () => <MarketplacePanel />,
};
