import { useEffect, useRef, useState } from "react";
import { getPluginCard, getPluginManifest } from "../api";
import { layoutRegistries } from "../kernel/seams";
import type { Card } from "../types";
import { FocusBlock } from "./FocusBlock";

type Widget = { id: string; title: string; layout: string; data: unknown };

// Shared fetch for the card plugin slots: asks every card-widget plugin OF THE GIVEN
// SLOT for this card's data (a plugin returning null shows nothing). Adding a
// card-widget plugin needs no edit anywhere here. Accepts null (no owning card, e.g. a
// card-less tmux in the Terminals tab) → no fetch, empty. Exported so a component that
// owns its own chrome (TerminalView's ⋯ menu) can fetch EARLY — at terminal mount, not
// first menu open — and render rows with zero pop-in.
// A provider that has not answered in this long is not going to make the card better;
// it is only holding a connection the rest of the page needs.
const WIDGET_TIMEOUT_MS = 5000;

export function usePluginWidgets(card: Card | null, slot: string): Widget[] {
  const [widgets, setWidgets] = useState<Widget[]>([]);
  // What is on screen right now, readable from inside the effect without making the
  // effect depend on it (which would refetch on its own result, forever).
  const shown = useRef<Widget[]>([]);
  shown.current = widgets;
  const prevCardId = useRef<string | null>(null);

  useEffect(() => {
    let stop = false;
    // Blank the section ONLY when this is a different card. A refetch of the SAME card
    // (its updated_at moved — a plugin's sync tick, a file arriving) used to clear
    // first and refill after, which unmounts every widget: React destroys the component
    // and with it any state it owns. That is why a half-typed message in a plugin's composer
    // vanished and the section appeared to reload every 30 seconds. Now the current
    // widgets stay mounted and are REPLACED in place as fresh data lands, so a
    // re-render is all that happens and local state survives.
    if (card?.id !== prevCardId.current) {
      setWidgets([]);
      shown.current = [];
      prevCardId.current = card?.id ?? null;
    }
    if (!card) return;
    const ctrl = new AbortController();
    getPluginManifest()
      .then((manifest) => {
        const ctx = { origin: card.origin, external_id: card.external_id, links: card.links };
        // Sorted by this slot's OWN effective order (orders.card_widget) — independent
        // of the plugin's nav/menu-bar order, user-reorderable from the manager
        // panel's "版面排序" → 卡片區塊 section.
        const mine = manifest
          .filter((p) => p.card_widget && (p.card_widget.slot ?? "body") === slot)
          .sort((a, b) => (a.orders?.card_widget ?? a.order) - (b.orders?.card_widget ?? b.order));
        // Filled by index so the rendered order is the (now slot-sorted) array's, not
        // whoever answered first — a section that reshuffles as its slower neighbours
        // arrive is worse than one that appears late.
        // Seeded from what is already showing, so a plugin that has not answered yet
        // keeps its current section instead of blinking out and back.
        const byId = new Map(shown.current.map((w) => [w.id, w]));
        const slots: (Widget | null)[] = mine.map((p) => byId.get(p.id) ?? null);

        // One slow provider used to hold the whole section: this waited on Promise.all,
        // so a 14s sandbox scan meant 14s before ANY widget appeared. Worse than the
        // wait itself, each pending POST holds one of HTTP/1.1's six connections per
        // origin, and the terminal's own requests queue behind them — which is how a
        // slow plugin made opening a tmux pane feel slow.
        const timer = setTimeout(() => ctrl.abort(), WIDGET_TIMEOUT_MS);
        let pending = mine.length;
        const settle = () => {
          if (--pending === 0) clearTimeout(timer);
        };

        mine.forEach((p, i) => {
          getPluginCard(p.id, ctx, ctrl.signal)
            .then((r) => {
              if (stop) return;
              // null now means "no longer applies" — drop the seeded copy, or a widget
              // whose data went away would linger until the card was closed.
              slots[i] =
                r.data == null
                  ? null
                  : {
                      id: p.id,
                      title: p.card_widget!.title,
                      layout: p.card_widget!.layout,
                      data: r.data,
                    };
              setWidgets(slots.filter((w): w is Widget => w != null));
            })
            .catch(() => {}) // abort or provider failure: that widget simply doesn't show
            .finally(settle);
        });
        if (!mine.length) clearTimeout(timer);
      })
      .catch(() => {});
    return () => {
      stop = true;
      ctrl.abort();
    };
    // re-fetch on updated_at too (not just id) so a widget appears live when the card's
    // data changes while the drawer is open — e.g. a file arriving via SendUserFile.
    // Providers are cheap / cached (sandbox has a 5s TTL) and only the open card refetches.
  }, [card?.id, card?.updated_at, slot]);

  return widgets;
}

// Titled sections for one body-ish slot. Three render points exist because WHERE a
// section sits relative to the card's own content is part of what it means: a "what
// happened last / what's needed from you" recap belongs ABOVE the description, a
// translation of that description belongs immediately BELOW it, and everything else
// belongs in the original slot further down. CardDetail drops one of these in per point.
export function PluginCardSlot({
  card,
  slot,
  focus,
  setFocus,
}: {
  card: Card;
  slot: string;
  // optional block-focus threading (see components/FocusBlock.tsx): when the
  // host (CardDetail) passes its focus state, every widget section gets the
  // same ⤢ affordance as the core sections. Absent → renders as before.
  focus?: string | null;
  setFocus?: (v: string | null) => void;
}) {
  const widgets = usePluginWidgets(card, slot);
  // resolved through the kernel seam (core provider = the glob-merged maps)
  const { CARD_LAYOUTS } = layoutRegistries();
  if (!widgets.length) return null;
  return (
    <>
      {widgets.map((w) => {
        const L = CARD_LAYOUTS[w.layout];
        if (!L) return null;
        const isFocused = focus === `plugin:${w.id}`;
        const body = (
          // focused: the widget's title already sits in the breadcrumb row, so
          // the <h3> would be a redundant second header — hidden; the section
          // flex-fills the overlay so a layout that honors `focused` (see
          // PluginCardProps) can pin its composer and grow its list.
          <section className={isFocused ? "flex-1 min-h-0 flex flex-col gap-2" : "space-y-2"}>
            {!isFocused && (
              <h3 className="text-xs font-semibold text-zinc-300 uppercase tracking-wide">{w.title}</h3>
            )}
            <L data={w.data} focused={isFocused} />
          </section>
        );
        return setFocus && focus !== undefined ? (
          <FocusBlock key={w.id} id={`plugin:${w.id}`} title={w.title} focus={focus} setFocus={setFocus}>
            {body}
          </FocusBlock>
        ) : (
          <span key={w.id} className="contents">{body}</span>
        );
      })}
    </>
  );
}

// slot="body" (the default): titled sections below the card content
export function PluginCardWidgets({
  card,
  focus,
  setFocus,
}: {
  card: Card;
  focus?: string | null;
  setFocus?: (v: string | null) => void;
}) {
  return <PluginCardSlot card={card} slot="body" focus={focus} setFocus={setFocus} />;
}

// slot="actions": chrome-less inline renders in the header action row (next to
// Snooze/Pin) — for compact controls like the stages plugin's move button.
export function PluginCardActions({ card }: { card: Card }) {
  const widgets = usePluginWidgets(card, "actions");
  const { CARD_LAYOUTS } = layoutRegistries();
  if (!widgets.length) return null;
  return (
    <>
      {widgets.map((w) => {
        const L = CARD_LAYOUTS[w.layout];
        return L ? <L key={w.id} data={w.data} /> : null;
      })}
    </>
  );
}

// (slot="menu" rows — the terminal ⋯ menu — are fetched by TerminalView itself via
// usePluginWidgets(card, "menu") at terminal mount, so opening the menu never pops in.)
