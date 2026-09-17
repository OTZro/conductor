import { createContext, type ReactNode, useCallback, useContext, useRef, useState } from "react";

// One place for transient feedback (see §3D). A toast can carry an Undo action —
// that's how non-destructive ops (Done/Snooze/Dismiss) avoid confirmation dialogs:
// act optimistically, offer 5s of Undo.

type Tone = "ok" | "err" | "info";
type Toast = { id: number; tone: Tone; msg: string; undo?: () => void };

type ToastApi = {
  toast: (msg: string, opts?: { tone?: Tone; undo?: () => void; ms?: number }) => void;
  error: (msg: string) => void;
};

const Ctx = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const api = useContext(Ctx);
  if (!api) throw new Error("useToast must be used within <ToastProvider>");
  return api;
}

// Opaque backgrounds: a top-right toast overlaps content, so a see-through fill
// (bg-*/10) reads as a glitch. Solid dark-tinted surfaces keep the tone but stay opaque.
const TONE_CLS: Record<Tone, string> = {
  ok: "border-sev-ok/50 bg-emerald-950 text-emerald-100",
  err: "border-sev-urgent/50 bg-red-950 text-red-100",
  info: "border-zinc-700 bg-zinc-900 text-zinc-100",
};

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<Toast[]>([]);
  const seq = useRef(0);

  const dismiss = useCallback((id: number) => setItems((xs) => xs.filter((t) => t.id !== id)), []);

  const toast = useCallback<ToastApi["toast"]>(
    (msg, opts) => {
      const id = ++seq.current;
      setItems((xs) => [...xs, { id, tone: opts?.tone ?? "info", msg, undo: opts?.undo }]);
      window.setTimeout(() => dismiss(id), opts?.ms ?? (opts?.undo ? 5000 : 3000));
    },
    [dismiss],
  );

  const error = useCallback<ToastApi["error"]>((msg) => toast(msg, { tone: "err", ms: 5000 }), [toast]);

  return (
    <Ctx.Provider value={{ toast, error }}>
      {children}
      {/* role=status + aria-live so a screen reader hears the toast (e.g. "Board
          refreshed"); pointer-events-none so the fixed stack never blocks clicks on the
          controls it overlaps (UserMenu dropdown, the card drawer's action row) — each
          toast re-enables its own events below. */}
      <div
        role="status"
        aria-live="polite"
        aria-atomic="false"
        className="pointer-events-none fixed top-14 right-3 z-[80] flex flex-col items-end gap-1.5 w-[min(92vw,26rem)]"
      >
        {items.map((t) => (
          <div
            key={t.id}
            className={`pointer-events-auto w-full flex items-center gap-2 text-body-s px-3 py-2 rounded-card border shadow-xl ${TONE_CLS[t.tone]}`}
          >
            <span className="flex-1">{t.msg}</span>
            {t.undo && (
              <button
                onClick={() => {
                  t.undo?.();
                  dismiss(t.id);
                }}
                className="shrink-0 font-semibold underline decoration-dotted hover:no-underline"
              >
                Undo
              </button>
            )}
            <button onClick={() => dismiss(t.id)} className="shrink-0 text-zinc-400 hover:text-zinc-200">
              ✕
            </button>
          </div>
        ))}
      </div>
    </Ctx.Provider>
  );
}
