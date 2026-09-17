import type { PluginMenuProps } from "../types";

// The "counts" menu-bar widget: the header lane-count chips (need-human / backlog /
// AI-working, plus any custom registry lane currently holding cards). Not
// self-contained like monitor/updater — it renders straight from `counts`/`lanes`,
// core's own board state, forwarded by PluginMenuBar. Renders nothing if `counts` is
// absent (e.g. an older core build that doesn't forward it yet).
export function CountsMenuBar({ counts, lanes }: PluginMenuProps) {
  if (!counts) return null;
  return (
    <>
      <span className="shrink-0 whitespace-nowrap text-state-human">★ {counts.get("need_human") ?? 0}</span>
      <span className="shrink-0 whitespace-nowrap text-zinc-400">bk {counts.get("backlog") ?? 0}</span>
      <span className="shrink-0 whitespace-nowrap text-state-ai">AI {counts.get("ai_working") ?? 0}</span>
      {/* custom (registry) stages get a chip only while they hold cards */}
      {(lanes ?? [])
        .filter((l) => !l.builtin && (counts.get(l.key) ?? 0) > 0)
        .map((l) => (
          <span key={l.key} className="shrink-0 whitespace-nowrap text-zinc-400">
            {l.title.toLowerCase()} {counts.get(l.key)}
          </span>
        ))}
    </>
  );
}
