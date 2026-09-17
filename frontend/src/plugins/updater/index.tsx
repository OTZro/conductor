import type { ComponentType } from "react";
import type { PluginMenuProps } from "../types";
import { UpdaterMenuBar } from "./UpdaterMenuBar";

// Updater plugin FE — a header-only widget (no tab), merged into the registry via
// import.meta.glob. Self-contained: it drives the plugin's own /api/update/* router.
export const MENU_LAYOUTS: Record<string, ComponentType<PluginMenuProps>> = {
  updater: UpdaterMenuBar,
};
