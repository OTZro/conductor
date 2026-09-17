import { useMemo } from "react";
import { needHumanRank, waitingHours } from "../cardMeta";
import { usePersisted } from "../persist";
import type { BoardLane, Card } from "../types";
import { type SortBy } from "./FilterPopover";
import { Lane } from "./Lane";

// Attention order (§3B.1): what needs you is first, the record of what's done is
// last (and collapsed). Backlog — not-yet-started — sits after in-flight work so
// the first screenful answers "what needs me now?".
//
// The lane SET is registry-driven (/api/board/lanes: built-ins + lanes.json + plugin
// stages) and arrives via the `lanes` prop; this static list is only the fallback
// while the registry loads / if the fetch fails, so the board never renders empty.
export const FALLBACK_LANES: BoardLane[] = [
  { key: "need_human", title: "Need Human", tone: "human", order: 0, rank: 0, within: "human", aging: true, collapsible: false, builtin: true },
  { key: "ai_working", title: "AI Working", tone: "ai", order: 10, rank: 20, within: "ai", aging: false, collapsible: false, builtin: true },
  { key: "backlog", title: "Backlog", tone: "backlog", order: 20, rank: 10, within: "human", aging: false, collapsible: true, builtin: true },
  { key: "done", title: "Done", tone: "done", order: 30, rank: 30, within: null, aging: false, collapsible: true, builtin: true },
];

function sortLane(key: Card["lane"], cards: Card[], sortBy: SortBy): Card[] {
  // Pinned jobs ride to the top of the lane, regardless of sort mode. Within that, an
  // explicit sort (title / id) applies to every lane; the default ("updated") keeps each
  // lane's natural order — Need Human by attention priority, the rest by recency.
  //
  // Keys are derived ONCE per card, not inside the comparator. `needHumanRank` walks the
  // cached blob and `waitingHours` reads the clock, and a comparator sees each card
  // O(log n) times — ~32k derivations for one 2817-card lane. Measured 1.96ms -> 0.50ms
  // on that shape, same resulting order.
  //
  // It is also a latent correctness fix. The old form read `Date.now()` TWICE inside one
  // comparison (`waitingHours(b) - waitingHours(a)`), so when those two reads straddle a
  // millisecond tick the comparator loses antisymmetry: cmp(a,b) and cmp(b,a) both come
  // back negative for cards with equal `updated_at`, which is an invalid comparator, and
  // Array.sort's contract does not survive one. Measured here: adjacent Date.now() calls
  // differ ~0.002% of the time, giving 1 violating pair in 66 in a tight loop — rare, but
  // ~32k comparisons run per sort of the big lane. (Under a forced monotonic clock it is
  // every pair, which is how a reviewer surfaced it.) One `now` for the whole sort makes
  // the comparator agree with itself by construction.
  //
  // WHY THIS MAY BE MEMOIZED on non-time deps: the only time-derived key is `waited`,
  // and it is used solely as `b.waited - a.waited`. Every card ages at the same rate, so
  // that difference is constant in `now` — the ORDER is time-invariant even though the
  // values are not. Break that (e.g. bucket into "overdue >24h") and this sort must
  // stop being memoized on `[cards, lanes, sortBy]` alone.
  const isNeedHuman = key === "need_human";
  const ranked = isNeedHuman && sortBy === "updated"; // the only mode reading rank/waited
  const now = Date.now();
  type Keyed = {
    card: Card;
    pin: number;
    rank: number;
    waited: number;
    updated: string;
  };
  const keyed: Keyed[] = cards.map((card) => ({
    card,
    pin: card.local.pinned ? 0 : 1,
    rank: ranked ? needHumanRank(card) : 0,
    waited: ranked ? waitingHours(card, now) : 0,
    updated: card.updated_at || "",
  }));
  const recent = (a: Keyed, b: Keyed) => b.updated.localeCompare(a.updated);
  const within = (a: Keyed, b: Keyed) => {
    if (sortBy === "title") return a.card.title.localeCompare(b.card.title);
    if (sortBy === "key") return a.card.external_id.localeCompare(b.card.external_id);
    if (ranked) return a.rank - b.rank || b.waited - a.waited || recent(a, b);
    return recent(a, b);
  };
  keyed.sort((a, b) => a.pin - b.pin || within(a, b));
  return keyed.map((k) => k.card);
}

export function Board({
  cards,
  lanes = FALLBACK_LANES,
  members,
  sortBy = "updated",
  onSelect,
}: {
  cards: Card[];
  lanes?: BoardLane[]; // registry-driven lane set (App fetches /api/board/lanes)
  members?: Map<string, Card[]>; // representative id → its grouped members (for the ⧉ badge)
  sortBy?: SortBy;
  onSelect: (id: string) => void;
}) {
  // per-lane collapse (persisted): Done starts collapsed, Backlog starts open; both toggle
  const [collapsed, setCollapsed] = usePersisted<Record<string, boolean>>("conductor.lanesCollapsed", {
    done: true,
  });
  // Bucket + sort once per lane, memoized. The memo is the point, not the bucketing:
  // measured at 2884 cards, per-lane `cards.filter()` and a single grouping pass cost
  // the same (~2.0ms) — the sort dominates, which is why sortLane precomputes its keys
  // above. What this buys is (a) skipping that work entirely on renders where no card
  // data changed, which is most of them, and (b) stable array identity per lane, which
  // is what lets the memo'd CardItem actually bail.
  const byLane = useMemo(() => {
    // seeded from `lanes` so a lane with no cards still gets an array; a card whose
    // lane isn't registered drops, same as the per-lane filter it replaced
    const buckets = new Map<string, Card[]>(lanes.map((l) => [l.key, []]));
    for (const c of cards) buckets.get(c.lane)?.push(c);
    // sorted into a second Map rather than re-`set`ting during `for…of buckets`:
    // replacing values mid-iteration is well-defined but reads as a hazard
    return new Map<string, Card[]>(
      lanes.map((l) => [l.key, sortLane(l.key, buckets.get(l.key) ?? [], sortBy)]),
    );
  }, [cards, lanes, sortBy]);

  // One horizontal, scroll-snapping row of stages on every viewport. The board pages
  // left/right between stages — snap-mandatory on phones so each swipe lands on a
  // stage (each lane is ~full-width with the next peeking at the edge, see Lane
  // min-w); free-scroll on desktop where several lanes fit at once. Cards scroll
  // vertically WITHIN a lane; the board itself never scrolls vertically.
  return (
    <div className="flex gap-3 p-3 h-full overflow-x-auto snap-x snap-mandatory sm:snap-none">
      {lanes.map((l) => (
        <Lane
          key={l.key}
          title={l.title}
          tone={l.tone}
          aging={l.aging}
          cards={byLane.get(l.key) ?? []}
          members={members}
          onSelect={onSelect}
          collapsed={l.collapsible ? !!collapsed[l.key] : false}
          onToggle={l.collapsible ? () => setCollapsed({ ...collapsed, [l.key]: !collapsed[l.key] }) : undefined}
        />
      ))}
    </div>
  );
}
