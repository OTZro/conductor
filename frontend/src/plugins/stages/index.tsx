import type { ComponentType } from "react";
import { StageMoveLayout } from "./StageMoveLayout";
import type { PluginCardProps } from "../types";

// merged into the FE layout registry via import.meta.glob (registry.tsx)
export const CARD_LAYOUTS: Record<string, ComponentType<PluginCardProps>> = {
  "stage-move": StageMoveLayout,
};
