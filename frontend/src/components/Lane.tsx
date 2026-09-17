import type { Card } from "../types";
import { EmptyState, StatusDot } from "../ui/primitives";
import { CardItem } from "./CardItem";

const TONE_DOT = { human: "human", ai: "ai", done: "done", backlog: "off" } as const;

export function Lane({
  title,
  tone,
  cards,
  members,
  onSelect,
  aging = false,
  collapsed = false,
  onToggle,
}: {
  title: string;
  tone: keyof typeof TONE_DOT;
  cards: Card[];
  members?: Map<string, Card[]>;
  onSelect: (id: string) => void;
  aging?: boolean; // registry flag: cards here show the waiting-hours aging accent
  collapsed?: boolean;
  onToggle?: () => void;
}) {
  // Collapsed (Done): a narrow spine showing just the count — it's a record, not work.
  if (collapsed) {
    return (
      <button
        onClick={onToggle}
        className="shrink-0 w-11 snap-start flex flex-col items-center gap-2 py-3 bg-surface-raised/40 rounded-card border border-zinc-800 text-zinc-400 hover:text-zinc-200 transition-colors duration-fast"
        title={`${title} (${cards.length}) — click to expand`}
      >
        <StatusDot tone={TONE_DOT[tone]} />
        <span className="text-body-s tabular-nums">{cards.length}</span>
        <span className="[writing-mode:vertical-rl] text-caption tracking-wide">{title}</span>
      </button>
    );
  }
  return (
    <div className="flex-1 min-w-[calc(100vw-5.5rem)] sm:min-w-[300px] snap-start flex flex-col bg-surface-raised/40 rounded-card border border-zinc-800">
      <button
        onClick={onToggle}
        disabled={!onToggle}
        className="flex items-center gap-2 px-3 py-2 border-b border-zinc-800 text-left disabled:cursor-default"
      >
        <StatusDot tone={TONE_DOT[tone]} />
        <h2 className="text-body font-semibold text-zinc-200">{title}</h2>
        <span className="ml-auto text-body-s text-zinc-500 tabular-nums">{cards.length}</span>
        {onToggle && <span className="text-zinc-500">▾</span>}
      </button>
      <div className="flex-1 overflow-y-auto p-2 space-y-2">
        {cards.map((c) => (
          <CardItem key={c.id} card={c} aging={aging} members={members?.get(c.id)} onSelect={onSelect} />
        ))}
        {!cards.length && (
          <EmptyState icon={tone === "human" ? "🎉" : "·"}>
            {tone === "human" ? "nothing needs you" : "empty"}
          </EmptyState>
        )}
      </div>
    </div>
  );
}
