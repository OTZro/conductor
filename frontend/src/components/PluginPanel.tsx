import { useEffect, useState } from "react";
import { getPluginTab, type TPluginManifest } from "../api";
import { layoutRegistries } from "../kernel/seams";
import type { Card } from "../types";

// Generic plugin tab: owns the poll + container/header, delegates the body to the named
// layout the plugin declared. Any plugin with a tab renders through here — no per-plugin
// view wiring in App.
const POLL_MS = 6000;

export function PluginPanel({
  plugin,
  cards,
  onOpenCard,
}: {
  plugin: TPluginManifest;
  cards: Card[];
  onOpenCard: (id: string) => void;
}) {
  const [data, setData] = useState<unknown>(null);
  const [err, setErr] = useState(false);
  // a self-contained tab (the orchestrator) renders a full component that owns its data;
  // there's nothing to fetch, so we skip the poll and render its layout immediately.
  const selfContained = !!plugin.tab?.self_contained;

  useEffect(() => {
    if (selfContained) return;
    let stop = false;
    const tick = () =>
      getPluginTab(plugin.id)
        .then((d) => {
          if (!stop) {
            setData(d);
            setErr(false);
          }
        })
        .catch(() => {
          if (!stop) setErr(true);
        });
    setData(null);
    tick();
    const t = setInterval(tick, POLL_MS);
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, [plugin.id, selfContained]);

  // resolved through the kernel seam (core provider = the glob-merged maps)
  const { TAB_LAYOUTS } = layoutRegistries();
  const Layout = plugin.tab ? TAB_LAYOUTS[plugin.tab.layout] : undefined;
  const cfg = plugin.tab?.config ?? {};
  // a self-contained tab owns its whole surface (its own header/scroll); render it bare.
  if (Layout && selfContained) return <Layout data={null} config={cfg} cards={cards} onOpenCard={onOpenCard} />;
  return (
    <div className="h-full overflow-y-auto bg-zinc-950 p-4">
      <div className="flex items-center gap-2 mb-4">
        <h2 className="text-sm font-semibold text-zinc-200">{plugin.label}</h2>
        <span className="text-[10px] text-zinc-600">plugin · refreshes every {POLL_MS / 1000}s</span>
        {err && <span className="text-[11px] text-red-400">fetch failed — retrying</span>}
      </div>
      {!Layout ? (
        <div className="text-[11px] text-amber-400">unknown layout: {plugin.tab?.layout ?? "(none)"}</div>
      ) : data != null ? (
        <Layout data={data} config={cfg} cards={cards} onOpenCard={onOpenCard} />
      ) : (
        <div className="text-[11px] text-zinc-600">loading…</div>
      )}
    </div>
  );
}
