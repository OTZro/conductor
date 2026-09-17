import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { addPin, createManual, listCards, patchState, setDone } from "./api";
import type { Card } from "./types";
import { useToast } from "./ui/toast";
import { connectWS } from "./ws";

// The data layer (§3E.1). Every board mutation invalidates the one `cards` query
// instead of each caller remembering which reload()s to fire — the class of bug
// that stranded killed sessions / un-refreshed prunes disappears structurally.

export const cardsKey = (showHidden: boolean) => ["cards", showHidden] as const;

export function useCards(showHidden: boolean, enabled: boolean) {
  return useQuery({
    queryKey: cardsKey(showHidden),
    queryFn: () => listCards(showHidden),
    enabled,
    refetchInterval: 15_000, // safety net; WS invalidation is the fast path
  });
}

/** Patch cards in every cached variant (hidden on/off) so an optimistic edit shows
 *  instantly regardless of which list is mounted. Returns a rollback snapshot. */
function useOptimisticCardPatch() {
  const qc = useQueryClient();
  return {
    async apply(id: string, patch: (c: Card) => Card) {
      await qc.cancelQueries({ queryKey: ["cards"] });
      const snapshots = qc.getQueriesData<Card[]>({ queryKey: ["cards"] });
      for (const [key, data] of snapshots) {
        if (data) qc.setQueryData(key, data.map((c) => (c.id === id ? patch(c) : c)));
      }
      return snapshots;
    },
    rollback(snapshots: [readonly unknown[], Card[] | undefined][]) {
      for (const [key, data] of snapshots) qc.setQueryData(key, data);
    },
    invalidate() {
      qc.invalidateQueries({ queryKey: ["cards"] });
    },
  };
}

export function useSetDone() {
  const opt = useOptimisticCardPatch();
  const { error } = useToast();
  return useMutation({
    mutationFn: ({ id, done }: { id: string; done: boolean }) => setDone(id, done),
    onMutate: ({ id, done }) =>
      opt.apply(id, (c) => ({ ...c, ball: done ? "none" : c.ball, lane: done ? "done" : c.lane })),
    onError: (e: any, _v, snapshots) => {
      if (snapshots) opt.rollback(snapshots as any);
      error(`Done failed: ${e?.message || e}`);
    },
    onSettled: opt.invalidate,
  });
}

export function usePatchState() {
  const opt = useOptimisticCardPatch();
  const { error } = useToast();
  return useMutation({
    mutationFn: ({ id, body }: { id: string; body: Record<string, unknown> }) => patchState(id, body),
    onMutate: ({ id, body }) =>
      opt.apply(id, (c) => ({
        ...c,
        local: {
          ...c.local,
          ...("dismissed" in body ? { dismissed: !!body.dismissed } : {}),
          ...("read" in body ? { read: !!body.read } : {}),
        },
      })),
    onError: (e: any, _v, snapshots) => {
      if (snapshots) opt.rollback(snapshots as any);
      error(`update failed: ${e?.message || e}`);
    },
    onSettled: opt.invalidate,
  });
}

export function useSnooze() {
  const opt = useOptimisticCardPatch();
  const { error } = useToast();
  return useMutation({
    // hours>0 → snooze that long; hours===0 → un-snooze
    mutationFn: ({ id, hours }: { id: string; hours: number }) =>
      patchState(id, hours > 0 ? { snooze_hours: hours } : { unsnooze: true }),
    onMutate: ({ id, hours }) =>
      opt.apply(id, (c) => ({
        ...c,
        local: {
          ...c.local,
          snoozed_until:
            hours > 0 ? new Date(Date.now() + hours * 3_600_000).toISOString() : null,
        },
      })),
    onError: (e: any, _v, snapshots) => {
      if (snapshots) opt.rollback(snapshots as any);
      error(`snooze failed: ${e?.message || e}`);
    },
    onSettled: opt.invalidate,
  });
}

export function useAddPin() {
  const qc = useQueryClient();
  const { error, toast } = useToast();
  return useMutation({
    mutationFn: (key: string) => addPin(key),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["cards"] });
      toast("pinned", { tone: "ok" });
    },
    onError: (e: any) => error(`pin failed: ${e?.message || e}`),
  });
}

export function useCreateManual() {
  const qc = useQueryClient();
  const { error } = useToast();
  return useMutation({
    mutationFn: (body: { title: string; summary?: string; url?: string; board?: string }) =>
      createManual(body),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["cards"] }),
    onError: (e: any) => error(`create failed: ${e?.message || e}`),
  });
}

/** Connect the WS and invalidate the board on any card-changing event. One place,
 *  debounced, replacing the per-component connect+reload wiring. Also refetches when
 *  the tab returns to the foreground — iOS kills background WS, so coming back from
 *  a locked phone would otherwise show stale data (§3F.4). */
export function useWsInvalidation(enabled: boolean, onStatus?: (c: boolean) => void) {
  const qc = useQueryClient();
  useEffect(() => {
    if (!enabled) return;
    let t: ReturnType<typeof setTimeout> | undefined;
    const disconnect = connectWS((ev) => {
      if (ev?.type === "card.upsert" || ev?.type === "card.pruned" || ev?.type === "done.pruned") {
        clearTimeout(t);
        t = setTimeout(() => qc.invalidateQueries({ queryKey: ["cards"] }), 400);
      }
    }, onStatus);
    const onVisible = () => {
      if (document.visibilityState === "visible") qc.invalidateQueries();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      disconnect();
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [enabled, qc, onStatus]);
}
