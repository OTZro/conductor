import { useEffect, useRef, useState } from "react";
import { Btn } from "../ui/primitives";

export type SortBy = "updated" | "title" | "key";

const SORTS: { v: SortBy; label: string }[] = [
  { v: "updated", label: "updated" },
  { v: "title", label: "title" },
  { v: "key", label: "ID" },
];

// One popover for every board filter (§3B.2) — the header no longer carries a row
// of selects/checkboxes. The trigger shows how many non-default filters are active.
export function FilterPopover(props: {
  // full source list from App: built-ins + present plugin/custom sources ({origin,label})
  sources: { origin: string; label: string }[];
  hidden: Set<string>; // origins toggled OFF (persisted)
  setHidden: (fn: (prev: Set<string>) => Set<string>) => void;
  sortBy: SortBy;
  setSortBy: (v: SortBy) => void;
  showHidden: boolean;
  setShowHidden: (v: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  // sort is not a filter — it never hides a card, so it must not bump the active-filter
  // count. Each hidden source counts as one active filter.
  const activeCount = props.hidden.size + (props.showHidden ? 1 : 0);

  const toggleOrigin = (o: string) =>
    props.setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(o)) next.delete(o); // was hidden → show again
      else next.add(o); // hide it
      return next;
    });

  return (
    <div className="relative" ref={ref}>
      <Btn variant={activeCount ? "accent" : "ghost"} onClick={() => setOpen((o) => !o)}>
        ⚙ filter{activeCount ? ` (${activeCount})` : ""}
      </Btn>
      {open && (
        <div className="absolute right-0 mt-1 z-40 w-64 rounded-modal bg-surface-raised border border-zinc-700 shadow-xl p-3 space-y-3 text-body-s">
          <div className="space-y-1">
            <span className="text-caption text-zinc-500">source</span>
            <div className="flex flex-wrap gap-1.5">
              {props.sources.map((s) => {
                const on = !props.hidden.has(s.origin); // ON = visible (not hidden)
                return (
                  <button
                    key={s.origin}
                    onClick={() => toggleOrigin(s.origin)}
                    className={`px-2 py-1 rounded-chip border transition-colors duration-fast ${
                      on
                        ? "bg-state-ai/20 border-state-ai/40 text-sky-200"
                        : "bg-surface-hover border-zinc-700 text-zinc-500"
                    }`}
                  >
                    {s.label}
                  </button>
                );
              })}
            </div>
          </div>

          <label className="block space-y-1">
            <span className="text-caption text-zinc-500">sort</span>
            <select
              value={props.sortBy}
              onChange={(e) => props.setSortBy(e.target.value as SortBy)}
              className="w-full px-2 py-1 rounded-chip bg-surface-hover border border-zinc-700 text-zinc-200"
            >
              {SORTS.map((s) => (
                <option key={s.v} value={s.v}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>

          <label className="flex items-center gap-2 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={props.showHidden}
              onChange={(e) => props.setShowHidden(e.target.checked)}
              className="accent-violet-500"
            />
            <span>💤 show snoozed / dismissed</span>
          </label>
        </div>
      )}
    </div>
  );
}
