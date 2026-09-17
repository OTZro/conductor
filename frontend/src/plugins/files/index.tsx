import type { ComponentType } from "react";
import { FilesCardLayout } from "./FilesCardLayout";
import type { PluginCardProps } from "../types";

// merged into the FE layout registry via import.meta.glob (registry.tsx)
export const CARD_LAYOUTS: Record<string, ComponentType<PluginCardProps>> = {
  "files-card": FilesCardLayout,
};
