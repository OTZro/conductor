/**
 * Core default providers, registered at app startup (side-effect import in
 * main.tsx, before the first render). This is the ONLY module that binds the
 * seams in seams.ts to the core implementations — consumers resolve through
 * the kernel and never import these components for the seamed surfaces.
 *
 * The defaults are the app's existing components, registered as-is at
 * priority 0: resolution hands back the very same component references the
 * call sites used to import directly, so the rendered tree — markup, props,
 * z-index, animation — is bit-for-bit what it was before the seam existed.
 * No alternate providers ship; a plugin (M7+) would register one at a higher
 * priority and dispose it to restore these.
 */

import { CardDetail } from "../components/CardDetail";
import { TerminalView } from "../components/TerminalView";
// side effect: runs the import.meta.glob layout merge AND registers the
// seeded maps as the "registry.layouts" provider (see plugins/registry.tsx)
import "../plugins/registry";
import type { CardSurfaceComponent, TerminalSurfaceComponent } from "./seams";
import { SURFACE_CARD, SURFACE_TERMINAL, kernel } from "./seams";

// The guard keeps a dev-server HMR re-evaluation of this module from tripping
// the registry's (deliberately loud) duplicate-provider check. It checks for
// the PRIORITY-0 row this block adds — NOT for emptiness: the plugins/registry
// import above runs plugin packages BEFORE this body, and a plugin registering
// its own surface.card provider (e.g. cardwindows at priority 100) would make
// the registry non-empty and an emptiness guard would silently skip the core
// defaults — no drawer, and getBase() resolving the plugin itself (the
// 2026-09-09 cardwindows incident). Presence of OUR row is the only correct
// idempotence check.
if (!kernel.services.providers(SURFACE_CARD).some((p) => p.priority === 0)) {
  kernel.services.register<CardSurfaceComponent>(SURFACE_CARD, CardDetail, { priority: 0 });
  kernel.services.register<TerminalSurfaceComponent>(SURFACE_TERMINAL, TerminalView, {
    priority: 0,
  });
}
