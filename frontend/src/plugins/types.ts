import type { BoardLane, Card } from "../types";

// A tab layout renders a plugin tab's data. `data` is layout-specific (the layout casts
// it); `cards` + `onOpenCard` let a layout cross-reference / open Conductor cards.
export type PluginTabProps = {
  data: unknown;
  config: Record<string, unknown>; // layout-specific manifest config, e.g. { url } for iframe
  cards: Card[];
  onOpenCard: (id: string) => void;
};

// A card-widget layout renders one plugin's card-detail data (already fetched, non-null).
export type PluginCardProps = {
  data: unknown;
  /** True while this widget's section is in BLOCK-FOCUS mode (the card-detail
   * ⤢, see components/FocusBlock.tsx): the section IS the whole card. A
   * layout may use it to skip its own internal accordion/collapse chrome and
   * flex-fill the height (message list grows, composer pins). Optional —
   * layouts that ignore it lose nothing. */
  focused?: boolean;
};

// A menu-bar layout renders in the header. Self-contained (fetches its own data); can
// jump to a view (e.g. its own tab) or open a specific card on click — a header widget
// that summarises per-card state needs to be able to take you TO that card.
//
// `counts`/`lanes` are CORE's own board state (per-lane card counts + the registry lane
// set), forwarded to every menu layout so a plugin like "counts" can render lane-count
// chips without re-deriving them. Optional: existing layouts (monitor/updater/fork)
// that don't reference them compile unchanged.
export type PluginMenuProps = {
  onOpenView: (id: string) => void;
  onOpenCard: (id: string) => void;
  counts?: Map<string, number>;
  lanes?: BoardLane[];
};
