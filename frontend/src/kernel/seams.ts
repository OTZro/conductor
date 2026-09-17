/**
 * The app's kernel instance + its seam vocabulary: service ids, seam
 * contracts, and the typed event map. This module is the FE analogue of the
 * backend's module-level `kernel` singleton — application code imports THIS
 * one instance; constructing private Kernels is for tests.
 *
 * Deliberately runtime-light: only type-level imports of app code (erased at
 * compile time), so importing seams.ts never drags React components or API
 * modules in. Default (core) providers are registered by ./bootstrap.ts at
 * app startup; consumers resolve through the helpers below and never import
 * an implementation.
 */

import type { ComponentType, ReactNode } from "react";
import type { PluginCardProps, PluginMenuProps, PluginTabProps } from "../plugins/types";
import type { Card } from "../types";
import { Kernel } from "./kernel";

// ── service ids ─────────────────────────────────────────────────────────────

/** How a selected card renders. Core default: the right-drawer CardDetail
 * flow (see bootstrap.ts). App.tsx resolves this instead of hardcoding. */
export const SURFACE_CARD = "surface.card";

/** How a card terminal host renders. Core default: the embedded TerminalView
 * flow; CardDetail's terminal call site resolves through this seam. */
export const SURFACE_TERMINAL = "surface.terminal";

/** The three plugin-layout maps as one kernel-held registry, seeded from the
 * existing import.meta.glob merge in plugins/registry.tsx (the authoring
 * format stays; the glob just feeds the registry). */
export const REGISTRY_LAYOUTS = "registry.layouts";

/** Always-mounted overlay surfaces — a COLLECTION, not a service: every
 * registered provider renders (App.tsx maps over surfaceOverlays() near its
 * modals layer), zero providers renders nothing (today's default). Overlays
 * own the z-[65] band: above the header (55) and the fullscreen terminal
 * (60), under the modals (70). Core registers no provider here. */
export const SURFACE_OVERLAY = "surface.overlay";

// ── seam contracts ──────────────────────────────────────────────────────────
// Written out structurally (not `ComponentProps<typeof CardDetail>`) on
// purpose: the CONTRACT is the seam's artifact, independent of the core
// implementation — bootstrap.ts registering the core component against these
// types is what keeps implementation and contract from drifting.

/** Contract for a "surface.card" provider — mirrors the core CardDetail
 * drawer's props exactly (the default provider IS that component). */
export type CardSurfaceProps = {
  card: Card;
  allCards?: Card[];
  width: number;
  onResize: (w: number) => void;
  onClose: () => void;
  onOpenCard?: (id: string) => void;
  onChanged: () => void;
  /** Hide the card's own header block (origin chip / id / title / action
   * row) — for compact hosts (e.g. a low workspace tile) whose chrome
   * re-renders the title and actions itself. Optional; the core drawer
   * never sets it, so default behavior is unchanged. */
  compactHeader?: boolean;
};
export type CardSurfaceComponent = ComponentType<CardSurfaceProps>;

/** Contract for a "surface.terminal" provider — mirrors the core
 * TerminalView's props exactly (the default provider IS that component). */
export type TerminalSurfaceProps = {
  url: string;
  sid: string | null;
  host?: string | null;
  fontSize?: number;
  fill?: boolean;
  onZoom?: (size: number) => void;
  onClose?: () => void;
  onDead?: () => void;
  onKill?: () => void;
  headerExtra?: ReactNode;
  menuExtra?: ReactNode;
  card?: Card | null;
  /** hide the toolbar row (host folds it behind its own accordion — see
   * CardDetail's focused-sessions mode); ignored while maximized. */
  hideHeader?: boolean;
};
export type TerminalSurfaceComponent = ComponentType<TerminalSurfaceProps>;

/** Contract for a "surface.overlay" collection item — an always-mounted layer
 * App renders alongside its modals. Gets the app-level data/callbacks an
 * overlay hosting card content needs (the same ones App passes the card
 * surface): the live card list, open-a-card, and the board-reload signal. */
export type OverlaySurfaceProps = {
  cards: Card[];
  onOpenCard: (id: string) => void;
  onChanged: () => void;
  /** Whether the CURRENT main view is board-like (the main board or a custom
   * dashboard) — App's own `boardish` flag. Overlays that visually claim
   * layout space (e.g. a workspace region) render only on boardish views and
   * yield on terminals/plugin tabs. */
  boardish: boolean;
};
export type OverlaySurfaceComponent = ComponentType<OverlaySurfaceProps>;

/** Contract for the "registry.layouts" provider — the three layout maps. The
 * core provider hands over the very objects plugins/registry.tsx merged, so
 * object identity (and therefore behavior) is unchanged. */
export type LayoutRegistries = {
  TAB_LAYOUTS: Record<string, ComponentType<PluginTabProps>>;
  CARD_LAYOUTS: Record<string, ComponentType<PluginCardProps>>;
  MENU_LAYOUTS: Record<string, ComponentType<PluginMenuProps>>;
};

// ── events ──────────────────────────────────────────────────────────────────

/** The app's event vocabulary (notify-dispatched from App.tsx at the existing
 * state transitions — pure additions, nothing consumes them yet; they are the
 * interception points a plugin would use).
 * - "card.open"   — a card became the selected card (also fires on a direct
 *                   card→card switch, without an intervening close).
 * - "card.close"  — the card drawer closed; `id` is the card that was open.
 * - "view.change" — the main view switched (board / terminals / plugin tab /
 *                   custom dashboard). */
export type AppEvents = {
  "card.open": { id: string };
  "card.close": { id: string };
  "view.change": { view: string; prev: string };
};

// ── the shared instance + typed resolvers ───────────────────────────────────

/** The one app kernel. Bootstrap registers core defaults into it; consumers
 * resolve through it; a plugin (M7+) would register overrides against it. */
export const kernel = new Kernel<AppEvents>();

/** Resolve the active card-surface component (throws if none registered). */
export const cardSurface = (): CardSurfaceComponent =>
  kernel.services.get<CardSurfaceComponent>(SURFACE_CARD);

/** Resolve the BASE (core default) card surface — the drawer CardDetail that
 * bootstrap registered at priority 0 — even while a plugin's alternate is the
 * active provider. For alternates that WRAP the default (e.g. a windowing
 * surface hosting the real CardDetail inside its own chrome). */
export const baseCardSurface = (): CardSurfaceComponent =>
  kernel.services.getBase<CardSurfaceComponent>(SURFACE_CARD);

/** Every registered overlay surface, registration order; [] when none. */
export const surfaceOverlays = (): OverlaySurfaceComponent[] =>
  kernel.collections.all<OverlaySurfaceComponent>(SURFACE_OVERLAY);

/** Resolve the active terminal-surface component. */
export const terminalSurface = (): TerminalSurfaceComponent =>
  kernel.services.get<TerminalSurfaceComponent>(SURFACE_TERMINAL);

/** Resolve the active layout registries. */
export const layoutRegistries = (): LayoutRegistries =>
  kernel.services.get<LayoutRegistries>(REGISTRY_LAYOUTS);
