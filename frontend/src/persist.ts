import { useCallback, useState } from "react";

// localStorage-backed state (§3E.4) — filters/sort/view shouldn't reset every
// reload. Same signature as useState.
export function usePersisted<T>(key: string, initial: T): [T, (v: T) => void] {
  const [val, setVal] = useState<T>(() => {
    try {
      const s = localStorage.getItem(key);
      if (s != null) return JSON.parse(s) as T;
    } catch {
      /* ignore corrupt value */
    }
    return initial;
  });
  const set = useCallback(
    (v: T) => {
      setVal(v);
      // Written HERE, not in an effect: an effect lands only after the next paint, and
      // callers dispatch events whose listeners read localStorage immediately — the
      // theme picker fires THEME_EVENT right after setTermFollows, and every terminal
      // respawns reading conductor.termFollowsTheme. With the deferred write they
      // respawned on the PREVIOUS value.
      try {
        localStorage.setItem(key, JSON.stringify(v));
      } catch {
        /* quota / private mode — non-fatal */
      }
    },
    [key],
  );
  return [val, set];
}

// ── per-card UI state ────────────────────────────────────────────────────────
// Section collapse belongs to the CARD, not to the app: collapsing a long Jira
// description on one ticket said nothing about the next one, yet a single global key
// meant closing a section on one card closed it on every card.
//
// One map for all cards and sections rather than a key per pair, and only DEVIATIONS
// from the default are written — so the map is the size of "cards where you actually
// changed something", not the size of the board. Capped anyway, by `at`, because cards
// outlive their entries: a card can be pruned or dismissed and its entry would
// otherwise sit in localStorage forever.
const CARD_UI_KEY = "conductor.cardUi";
const CARD_UI_MAX = 200;

// `s` holds whatever a section stores — a collapse flag, the member you were talking
// to on this card. Kept as unknown rather than boolean so one store and one eviction
// policy serve both; a stale value of the wrong shape just fails the typeof check
// below and falls back to the default.
type CardUi = Record<string, { at: number; s: Record<string, unknown> }>;

function readCardUi(): CardUi {
  try {
    const raw = JSON.parse(localStorage.getItem(CARD_UI_KEY) || "{}");
    return raw && typeof raw === "object" ? (raw as CardUi) : {};
  } catch {
    return {};
  }
}

function writeCardUi(map: CardUi): void {
  const ids = Object.keys(map);
  if (ids.length > CARD_UI_MAX) {
    // evict by timestamp, NOT by key order: object key order is only insertion order
    // for non-integer-like keys, and a card id is not guaranteed to be one.
    ids
      .sort((a, b) => (map[b].at || 0) - (map[a].at || 0))
      .slice(CARD_UI_MAX)
      .forEach((id) => delete map[id]);
  }
  try {
    localStorage.setItem(CARD_UI_KEY, JSON.stringify(map));
  } catch {
    /* quota / private mode — non-fatal */
  }
}

/**
 * State scoped to ONE card — collapse flags, the person you were talking to there.
 * Same signature as useState.
 *
 * `fallback` is what an unvisited card starts from, and is what makes a per-card
 * memory usable rather than merely correct: the first time you open a card there is
 * nothing stored for it, and an empty picker on every new card is the friction the
 * memory was supposed to remove. Pass the global last-used value there.
 */
export function usePerCard<T>(
  section: string,
  cardId: string,
  initial: T,
  fallback?: T,
): [T, (v: T) => void] {
  const start = (): T => {
    // No card id means no per-card identity: persisting under "" would file every such
    // caller in ONE bucket, which is precisely the cross-card leak this hook exists to
    // prevent. Fall back to plain component state.
    if (!cardId) return fallback ?? initial;
    const stored = readCardUi()[cardId]?.s?.[section];
    if (stored !== undefined && typeof stored === typeof initial) return stored as T;
    return fallback ?? initial;
  };
  const [val, setVal] = useState<T>(start);
  const [seenCard, setSeenCard] = useState(cardId);

  // CardDetail is NOT keyed by card id, so switching cards reuses this component and
  // useState's initializer never runs again — without resetting, the previous card's
  // value sticks.
  //
  // Adjusted DURING RENDER rather than in an effect, which is React's documented
  // pattern for exactly this. An effect runs AFTER the browser has been given a frame,
  // so the first paint of the new card showed the OLD card's value and only then
  // corrected itself: open a card whose terminal you had collapsed, switch to one you
  // had not, and it appeared collapsed too. That flash is indistinguishable from the
  // state having leaked between cards, and on a section whose content is expensive to
  // mount it is not even brief.
  if (seenCard !== cardId) {
    setSeenCard(cardId);
    setVal(start());
  }

  const set = useCallback(
    (v: T) => {
      setVal(v);
      if (!cardId) return;
      const map = readCardUi();
      const entry = { ...(map[cardId]?.s || {}) };
      if (v === initial) delete entry[section];
      else entry[section] = v;
      if (Object.keys(entry).length) map[cardId] = { at: Date.now(), s: entry };
      else delete map[cardId];
      writeCardUi(map);
    },
    [cardId, section, initial],
  );

  return [val, set];
}

