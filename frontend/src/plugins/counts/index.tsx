import type { ComponentType } from "react";
import type { PluginMenuProps } from "../types";
import { CountsMenuBar } from "./CountsMenuBar";

// Counts plugin FE — a single menu-bar widget, merged into the registry via
// import.meta.glob. Not self-contained: it renders from the counts/lanes core forwards
// through PluginMenuBar, rather than fetching its own data.
export const MENU_LAYOUTS: Record<string, ComponentType<PluginMenuProps>> = {
  counts: CountsMenuBar,
};
