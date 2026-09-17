import { useState } from "react";
import { patchState } from "../../api";
import { HoverCard } from "../../ui/HoverCard";
import { Btn } from "../../ui/primitives";
import type { PluginCardProps } from "../types";

// The manual "move to stage" control — plugin UI over core's stage machinery
// (PATCH /api/cards/{id}/state {stage}; core validates against the registry).
// Rendered in the card detail's ACTION ROW (card_widget slot="actions"): a compact
// ⇢ button whose hover panel lists the human-space stages + Auto. Optimistic: the
// label/✓ track the click and snap back on rejection; the board's actual lane change
// arrives via the normal WS invalidation.
type StageData = {
  card_id: string;
  current: string | null;
  choices: { key: string; title: string }[];
};

export function StageMoveLayout({ data }: PluginCardProps) {
  const { card_id, current, choices } = data as StageData;
  const [sel, setSel] = useState<string | null>(current);
  const [busy, setBusy] = useState(false);

  const move = async (stage: string | null) => {
    const prev = sel;
    setSel(stage);
    setBusy(true);
    try {
      await patchState(card_id, { stage });
    } catch {
      setSel(prev); // rejected (unknown stage / offline) → snap back
    } finally {
      setBusy(false);
    }
  };

  const item = "px-2 py-1 rounded text-left text-xs text-zinc-200 hover:bg-zinc-800 disabled:opacity-50";
  const label = sel ? choices.find((c) => c.key === sel)?.title || sel : "Stage";
  return (
    <HoverCard
      width={170}
      content={
        <div className="flex flex-col gap-0.5">
          <div className="px-1 pb-1 text-[10px] uppercase tracking-wider text-zinc-400">
            move to stage…
          </div>
          <button disabled={busy} className={item} onClick={() => move(null)}>
            Auto (derived){sel == null && " ✓"}
          </button>
          {choices.map((c) => (
            <button key={c.key} disabled={busy} className={item} onClick={() => move(c.key)}>
              {c.title}
              {sel === c.key && " ✓"}
            </button>
          ))}
        </div>
      }
    >
      <Btn variant="ghost">⇢ {label}</Btn>
    </HoverCard>
  );
}
