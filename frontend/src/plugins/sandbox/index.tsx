import type { ComponentType } from "react";
import type { PluginCardProps, PluginTabProps } from "../types";
import { SandboxCardLayout } from "./SandboxCardLayout";
import { SandboxHostsLayout } from "./SandboxHostsLayout";

// Sandbox plugin FE — its layouts, merged into the registry via import.meta.glob.
export const TAB_LAYOUTS: Record<string, ComponentType<PluginTabProps>> = {
  "sandbox-hosts": SandboxHostsLayout,
};
export const CARD_LAYOUTS: Record<string, ComponentType<PluginCardProps>> = {
  "sandbox-card": SandboxCardLayout,
};
