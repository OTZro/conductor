import { type TPluginManifest } from "../api";
import { layoutRegistries } from "../kernel/seams";
import type { BoardLane } from "../types";

// The menu-bar (header) plugin slot: renders every plugin that declares a menu_bar
// contribution, via its named layout. Drop this once in the header; adding a menu
// widget needs no edit here.
//
// `counts`/`lanes` are core's board state, forwarded to every layout so a plugin (e.g.
// "counts") can render lane-count chips without re-deriving them itself. Optional on
// PluginMenuProps, so passing them here is safe even for layouts that ignore them.
export function PluginMenuBar({
  plugins,
  onOpenView,
  onOpenCard,
  counts,
  lanes,
}: {
  plugins: TPluginManifest[];
  onOpenView: (id: string) => void;
  onOpenCard: (id: string) => void;
  counts?: Map<string, number>;
  lanes?: BoardLane[];
}) {
  // Layouts resolved through the kernel seam — the core provider hands back the same
  // glob-merged maps plugins/registry.tsx always built (see kernel/README.md).
  const { MENU_LAYOUTS } = layoutRegistries();
  // Sorted by this slot's OWN effective order (orders.menu_bar) — independent of the
  // nav's tab order and the card-widget order, since a plugin can rank differently in
  // each (see TPluginManifest.orders). User-set via the manager panel's "版面排序" →
  // Menu bar section; Array.prototype.sort is stable, so ties keep manifest order.
  const items = plugins.filter((p) => p.menu_bar).sort((a, b) => (a.orders?.menu_bar ?? a.order) - (b.orders?.menu_bar ?? b.order));
  if (!items.length) return null;
  return (
    <>
      {items.map((p) => {
        const L = p.menu_bar ? MENU_LAYOUTS[p.menu_bar.layout] : undefined;
        return L ? (
          <L
            key={p.id}
            onOpenView={onOpenView}
            onOpenCard={onOpenCard}
            counts={counts}
            lanes={lanes}
          />
        ) : null;
      })}
    </>
  );
}
