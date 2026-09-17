import type { ComponentType } from "react";
import type { PluginMenuProps, PluginTabProps } from "../types";
import { MonitorMenuBar } from "./MonitorMenuBar";
import { MonitorPanel } from "./MonitorPanel";

// Monitor plugin FE — a self-contained tab + a menu-bar widget, merged into the registry
// via import.meta.glob. Self-contained: the panel drives its own /api/metrics client.
export const TAB_LAYOUTS: Record<string, ComponentType<PluginTabProps>> = {
  monitor: () => <MonitorPanel />,
};
export const MENU_LAYOUTS: Record<string, ComponentType<PluginMenuProps>> = {
  "monitor-hosts": MonitorMenuBar,
};
