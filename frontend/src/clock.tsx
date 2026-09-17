import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";

// A shared, coarse clock for everything that renders a *relative* time.
//
// This exists because of memo(). A card whose JSON hasn't changed keeps its object
// identity across a refetch (react-query structural sharing), so a memo'd CardItem
// correctly skips re-rendering — and that would freeze every value derived from the
// wall clock: the "now / 5m / 2h" stamp, a live worker's elapsed time, the due-date
// tone, the aging border, whether a snooze has expired. A card labelled "now" would
// still say "now" an hour later, because nothing about the card changed — only time did.
//
// `useNow()` RETURNS the instant, and the helpers in cardMeta.ts take it as a required
// argument, so the subscription is load-bearing rather than decorative: drop the call
// and the code stops compiling instead of silently freezing. An earlier revision
// subscribed for the side effect alone and discarded the value — three reviewers
// independently flagged that a "remove unused call" cleanup would restore the bug and
// look correct doing it.
//
// A context rather than a prop: context updates reach consumers THROUGH memo, so no
// intermediate component (Board, Lane) threads a tick it doesn't itself use.
//
// The value is quantized to `intervalMs` rather than a raw timestamp, so it changes
// once per interval; React bails on identical state and consumers re-render exactly as
// often as their displayed text can change. Note this is a whole-context value: every
// consumer re-renders on a tick, which for a long lane is real work once a minute. That
// is a 4x reduction against the 15s refetch cadence it replaces, not an elimination —
// per-consumer bail-out would need useSyncExternalStore with a card-specific snapshot.
// null, not a timestamp: a consumer mounted outside the provider must fail LOUDLY.
// A module-load default would render a plausible-looking time frozen at page load,
// which is the silent-staleness failure this whole module exists to prevent.
const ClockContext = createContext<number | null>(null);

function quantize(ms: number, intervalMs: number): number {
  return Math.floor(ms / intervalMs) * intervalMs;
}

export function ClockProvider({
  children,
  intervalMs = 60_000,
}: {
  children: ReactNode;
  intervalMs?: number;
}) {
  const [now, setNow] = useState(() => quantize(Date.now(), intervalMs));
  const sync = useCallback(() => setNow(quantize(Date.now(), intervalMs)), [intervalMs]);

  useEffect(() => {
    let align: ReturnType<typeof setTimeout> | undefined;
    let id: ReturnType<typeof setInterval> | undefined;
    const disarm = () => {
      if (align !== undefined) clearTimeout(align);
      if (id !== undefined) clearInterval(id);
      align = undefined;
      id = undefined;
    };
    // First fire on the next bucket BOUNDARY, then every interval. Starting the
    // interval from mount time leaves the displayed value up to a full interval behind
    // the bucket it names — a stamp that should read "6m" keeps saying "5m" until the
    // offset comes round.
    const arm = () => {
      disarm();
      align = setTimeout(() => {
        align = undefined;
        sync();
        id = setInterval(sync, intervalMs);
      }, intervalMs - (Date.now() % intervalMs));
    };
    arm();
    // Resync on wake. Background tabs get their timers throttled (Chrome clamps to
    // ~1/min) or suspended outright (iOS Safari), so an interval alone leaves stamps
    // reading whatever they said when the tab went away. queries.ts does the same for
    // the data path (§3F.4) — but that only re-renders cards whose JSON actually
    // changed, and before memo() it was the whole-board re-render, not the refetch,
    // that happened to refresh the clock. Nothing carries that now except this.
    //
    // Re-`arm()`, not just `sync()`: a throttled interval survives the background
    // with an arbitrary PHASE, so every later bucket flip lands up to a full interval
    // late for the rest of the session. Correcting the value without correcting the
    // phase fixes the wake and leaves the drift.
    const rearm = () => {
      sync();
      arm();
    };
    const onWake = () => {
      if (document.visibilityState === "visible") rearm();
    };
    document.addEventListener("visibilitychange", onWake);
    window.addEventListener("focus", rearm);
    return () => {
      disarm();
      document.removeEventListener("visibilitychange", onWake);
      window.removeEventListener("focus", rearm);
    };
  }, [intervalMs, sync]);

  return <ClockContext.Provider value={now}>{children}</ClockContext.Provider>;
}

/** The current instant, quantized to the provider's interval. Pass it to the cardMeta
 *  helpers — they require it, which is what keeps a memo'd component's relative times
 *  from freezing. */
export function useNow(): number {
  const now = useContext(ClockContext);
  if (now === null) {
    throw new Error("useNow() requires a <ClockProvider> ancestor (mounted in main.tsx)");
  }
  return now;
}
